"""Audio file tagging module with container-specific handlers.

This module provides a clean, type-safe interface for tagging audio files
across different container formats (FLAC, MP3, M4A, OGG, Opus).
"""

import base64
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import humanfriendly
import msgspec
from mutagen.easyid3 import EasyID3
from mutagen.easymp4 import EasyMP4
from mutagen.flac import FLAC, Picture, VCFLACDict
from mutagen.id3 import ID3
from mutagen.id3._frames import APIC, COMM, TDAT, TPUB, USLT
from mutagen.id3._specs import PictureType
from mutagen.mp3 import MP3, EasyMP3
from mutagen.mp4 import MP4Cover
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis
from PIL import Image

from .utils.exceptions import TagSavingFailure
from .utils.models import ContainerEnum, CreditsInfo, TrackInfo

logger = logging.getLogger(__name__)


@runtime_checkable
class SupportsTagging(Protocol):
    """Protocol for objects that support basic tagging operations."""

    def __setitem__(self, key: str, value: object) -> None: ...
    def __getitem__(self, key: str) -> object: ...
    def save(self) -> None: ...


class TaggingContext(msgspec.Struct):
    """Context object containing all tagging-related data.

    Attributes:
        file_path: Path to the audio file.
        image_path: Path to the cover image, or None if no cover.
        track_info: Track metadata information.
        credits_list: List of credits information.
        embedded_lyrics: Lyrics to embed in the file.
        container: Audio container format.
    """

    file_path: Path
    image_path: Path | None
    track_info: TrackInfo
    credits_list: list[CreditsInfo]
    embedded_lyrics: str
    container: ContainerEnum


class BaseTagger(ABC):
    """Abstract base class for container-specific taggers."""

    def __init__(self, ctx: TaggingContext) -> None:
        """Initializes the tagger with context.

        Args:
            ctx: Tagging context containing all necessary data.
        """
        self.ctx = ctx
        self.tagger = self._create_tagger()

    @abstractmethod
    def _create_tagger(self) -> SupportsTagging:
        """Creates the container-specific tagger instance."""
        ...

    @abstractmethod
    def _set_track_disc_numbers(self) -> None:
        """Sets track and disc number tags in container-specific format."""
        ...

    def _set_track_disc_numbers_separate(self) -> None:
        """Sets track/disc numbers as separate fields (FLAC/OGG style)."""
        tags = self.ctx.track_info.tags
        if tags.track_number:
            self.tagger["tracknumber"] = str(tags.track_number)
        if tags.disc_number:
            self.tagger["discnumber"] = str(tags.disc_number)
        if tags.total_tracks:
            self.tagger["totaltracks"] = str(tags.total_tracks)
        if tags.total_discs:
            self.tagger["totaldiscs"] = str(tags.total_discs)

    def _set_track_disc_numbers_combined(self) -> None:
        """Sets track/disc numbers in combined format (MP3/M4A style)."""
        tags = self.ctx.track_info.tags
        if tags.track_number and tags.total_tracks:
            self.tagger["tracknumber"] = f"{tags.track_number}/{tags.total_tracks}"
        elif tags.track_number:
            self.tagger["tracknumber"] = str(tags.track_number)

        if tags.disc_number and tags.total_discs:
            self.tagger["discnumber"] = f"{tags.disc_number}/{tags.total_discs}"
        elif tags.disc_number:
            self.tagger["discnumber"] = str(tags.disc_number)

    def _set_release_date(self) -> None:
        """Sets release date tag in container-specific format."""
        track = self.ctx.track_info
        if track.tags.release_date:
            self.tagger["date"] = track.tags.release_date
        else:
            self.tagger["date"] = str(track.release_year)

    def _set_explicit(self) -> None:
        """Sets explicit rating tag in container-specific format."""
        if self.ctx.track_info.explicit is not None:
            value = "Explicit" if self.ctx.track_info.explicit else "Clean"
            self.tagger["Rating"] = value

    def _set_isrc_upc(self) -> None:
        """Sets ISRC and UPC tags in container-specific format."""
        tags = self.ctx.track_info.tags
        if tags.isrc:
            self.tagger["isrc"] = tags.isrc
        if tags.upc:
            self.tagger["UPC"] = tags.upc

    def _set_label(self) -> None:
        """Sets label tag in container-specific format."""
        if self.ctx.track_info.tags.label:
            self.tagger["Label"] = self.ctx.track_info.tags.label

    @abstractmethod
    def _set_credits(self) -> None:
        """Sets credits tags in container-specific format."""
        ...

    def _set_lyrics(self) -> None:
        """Sets lyrics tag in container-specific format."""
        if self.ctx.embedded_lyrics:
            self.tagger["lyrics"] = self.ctx.embedded_lyrics

    @abstractmethod
    def _embed_cover(self, data: bytes) -> None:
        """Embeds cover art in container-specific format."""
        ...

    @abstractmethod
    def _save(self) -> None:
        """Saves tags in container-specific format."""
        ...

    def _set_common_tags(self) -> None:
        """Sets common tags shared across all containers."""
        track = self.ctx.track_info
        self.tagger["title"] = track.name

        if track.album:
            self.tagger["album"] = track.album
        if track.tags.album_artist:
            self.tagger["albumartist"] = track.tags.album_artist

        self.tagger["artist"] = track.artists

        if track.tags.genres:
            self.tagger["genre"] = track.tags.genres
        if track.tags.copyright:
            self.tagger["copyright"] = track.tags.copyright

    def _set_replay_gain(self) -> None:
        """Sets replay gain tags if available."""
        track = self.ctx.track_info
        if track.tags.replay_gain and track.tags.replay_peak:
            self.tagger["REPLAYGAIN_TRACK_GAIN"] = str(track.tags.replay_gain)
            self.tagger["REPLAYGAIN_TRACK_PEAK"] = str(track.tags.replay_peak)

    def _handle_cover(self) -> None:
        """Handles cover art embedding with size restrictions."""
        if not self.ctx.image_path:
            return

        with open(str(self.ctx.image_path), "rb") as f:
            data = f.read()

        # Hard safety cap: most containers/players reject covers above ~16 MiB.
        max_size = 1023 * 1024 * 16

        if len(data) < max_size:
            self._embed_cover(data)
        else:
            logger.warning(
                "Cover file size is too large, only %s are allowed. "
                "Track will not have cover saved.",
                humanfriendly.format_size(max_size, binary=True),
            )

    def tag(self) -> None:
        """Executes the full tagging process."""
        self._set_common_tags()
        self._set_track_disc_numbers()
        self._set_release_date()
        self._set_explicit()
        self._set_isrc_upc()
        self._set_label()
        self._set_credits()
        self._set_lyrics()
        self._set_replay_gain()
        self._handle_cover()

        try:
            self._save()
        except (OSError, ValueError, RuntimeError) as e:
            self._handle_save_failure(e)

    def _handle_save_failure(self, error: Exception) -> None:
        """Handles tagging failure by saving tags to text file."""
        logging.debug("Tagging failed.")
        track = self.ctx.track_info

        tag_text = "\n".join(
            f"{k}: {v}"
            for k, v in msgspec.structs.asdict(track.tags).items()
            if v and k not in {"credits", "lyrics"}
        )

        if self.ctx.credits_list:
            tag_text += "\n\ncredits:\n    " + "\n    ".join(
                f"{credit.type}: {', '.join(credit.names)}"
                for credit in self.ctx.credits_list
                if credit.names
            )

        if self.ctx.embedded_lyrics:
            tag_text += "\n\nlyrics:\n    " + "\n    ".join(
                self.ctx.embedded_lyrics.split("\n")
            )

        txt_path = self.ctx.file_path.parent / f"{self.ctx.file_path.stem}_tags.txt"
        with open(str(txt_path), "w", encoding="utf-8") as f:
            f.write(tag_text)

        raise TagSavingFailure from error

    def _clean_dash_tags(self, tags: Any) -> None:
        """Removes useless MPEG-DASH tags from audio file tags.

        Args:
            tags: The tags object (can be VCFLACDict, OggVorbis/OggOpus tags,
                or EasyMP4 tags).
        """
        if tags is None:
            return
        for tag in ("major_brand", "minor_version", "compatible_brands", "encoder"):
            if tag in tags:
                del tags[tag]


class FLACTagger(BaseTagger):
    """Tagger implementation for FLAC files."""

    def _create_tagger(self) -> FLAC:
        tagger = FLAC(str(self.ctx.file_path))
        if isinstance(tagger.tags, VCFLACDict):
            self._clean_dash_tags(tagger.tags)
        return tagger

    def _set_track_disc_numbers(self) -> None:
        self._set_track_disc_numbers_separate()

    def _set_credits(self) -> None:
        if not self.ctx.credits_list:
            return
        tagger = self.tagger
        if not isinstance(tagger, FLAC):
            return
        for credit in self.ctx.credits_list:
            if isinstance(tagger.tags, VCFLACDict) and credit.names:
                tagger.tags[credit.type] = credit.names

    def _set_extra_tags(self) -> None:
        """Sets extra custom tags for FLAC files."""
        for key, value in self.ctx.track_info.tags.extra_tags.items():
            self.tagger[key] = value

    def _embed_cover(self, data: bytes) -> None:
        tagger = self.tagger
        if not isinstance(tagger, FLAC):
            return
        picture = Picture()
        picture.data = data
        picture.type = PictureType.COVER_FRONT
        picture.mime = "image/jpeg"
        tagger.add_picture(picture)

    def _save(self) -> None:
        self.tagger.save()

    def tag(self) -> None:
        """Executes the full tagging process for FLAC."""
        self._set_extra_tags()
        super().tag()


class OggBaseTagger(BaseTagger, ABC):
    """Base tagger for OGG-based formats (Vorbis, Opus)."""

    def _set_track_disc_numbers(self) -> None:
        self._set_track_disc_numbers_separate()

    def _set_credits(self) -> None:
        if not self.ctx.credits_list:
            return
        tagger = self.tagger
        if not isinstance(tagger, (OggVorbis, OggOpus)):
            return
        for credit in self.ctx.credits_list:
            if tagger.tags is not None and credit.names:
                tagger.tags[credit.type] = credit.names

    def _set_extra_tags(self) -> None:
        """Sets extra custom tags."""
        for key, value in self.ctx.track_info.tags.extra_tags.items():
            self.tagger[key] = value

    def _embed_cover(self, data: bytes) -> None:
        """Embeds cover using metadata_block_picture for OGG formats."""
        picture = Picture()
        picture.data = data
        picture.type = PictureType.COVER_FRONT
        picture.desc = "Cover Art"
        picture.mime = "image/jpeg"

        if self.ctx.image_path:
            im = Image.open(str(self.ctx.image_path))
            picture.width, picture.height = im.size
            picture.depth = 24

        encoded_data = base64.b64encode(picture.write())
        self.tagger["metadata_block_picture"] = [encoded_data.decode("ascii")]

    def _save(self) -> None:
        self.tagger.save()

    def tag(self) -> None:
        """Executes the full tagging process."""
        self._set_extra_tags()
        super().tag()


class OggVorbisTagger(OggBaseTagger):
    """Tagger implementation for OGG Vorbis files."""

    def _create_tagger(self) -> OggVorbis:
        tagger = OggVorbis(str(self.ctx.file_path))
        self._clean_dash_tags(tagger.tags)
        return tagger


class OpusTagger(OggBaseTagger):
    """Tagger implementation for Opus files."""

    def _create_tagger(self) -> OggOpus:
        tagger = OggOpus(str(self.ctx.file_path))
        self._clean_dash_tags(tagger.tags)
        return tagger


class MP3Tagger(BaseTagger):
    """Tagger implementation for MP3 files using low-level ID3 API.

    Uses MP3 with ID3 tags directly instead of EasyMP3 to have full control
    over ID3 frames like APIC, USLT, TDAT, etc.
    """

    def __init__(self, ctx: TaggingContext) -> None:
        """Initializes the MP3 tagger.

        Args:
            ctx: Tagging context containing all necessary data.
        """
        self.ctx = ctx
        self._mp3 = MP3(str(ctx.file_path), ID3=ID3)
        if self._mp3.tags is None:
            self._mp3.add_tags()
        self._id3 = self._mp3.tags
        # Create EasyID3 wrapper for simple tags
        self._easy = EasyMP3(str(ctx.file_path))
        if self._easy.tags is None:
            self._easy.tags = EasyID3()
        self._register_easy_keys()
        self._clean_dash_tags(self._easy.tags)
        self.tagger = self._easy

    def _register_easy_keys(self) -> None:
        """Registers custom keys for EasyID3."""
        if not isinstance(self._easy.tags, EasyID3):
            return
        self._easy.tags.RegisterTextKey("encoded", "TSSE")
        self._easy.tags.RegisterTXXXKey("compatible_brands", "compatible_brands")
        self._easy.tags.RegisterTXXXKey("major_brand", "major_brand")
        self._easy.tags.RegisterTXXXKey("minor_version", "minor_version")
        self._easy.tags.RegisterTXXXKey("Rating", "Rating")
        self._easy.tags.RegisterTXXXKey("upc", "BARCODE")
        self._easy.tags.pop("encoded", None)

    def _create_tagger(self) -> EasyMP3:
        # Not used, initialization is done in __init__
        return self._easy

    def _set_track_disc_numbers(self) -> None:
        self._set_track_disc_numbers_combined()

    def _set_release_date(self) -> None:
        track = self.ctx.track_info
        if track.tags.release_date:
            # Convert YYYY-MM-DD to DDMM format for TDAT
            release_dd_mm = (
                f"{track.tags.release_date[8:10]}{track.tags.release_date[5:7]}"
            )
            if self._id3 is not None:
                self._id3.add(TDAT(encoding=3, text=release_dd_mm))
            self.tagger["date"] = str(track.release_year)
        else:
            self.tagger["date"] = str(track.release_year)

    def _set_label(self) -> None:
        if not self.ctx.track_info.tags.label:
            return
        if self._id3 is not None:
            self._id3.add(TPUB(encoding=3, text=self.ctx.track_info.tags.label))

    def _set_credits(self) -> None:
        if not self.ctx.credits_list:
            return
        if not isinstance(self._easy.tags, EasyID3):
            return
        for credit in self.ctx.credits_list:
            self._easy.tags.RegisterTXXXKey(credit.type.upper(), credit.type)
            self.tagger[credit.type] = credit.names

    def _set_lyrics(self) -> None:
        if not self.ctx.embedded_lyrics:
            return
        if self._id3 is not None:
            self._id3.add(USLT(encoding=3, lang="eng", text=self.ctx.embedded_lyrics))

    def _set_comment(self) -> None:
        """Sets comment tag for MP3."""
        if not self.ctx.track_info.tags.comment:
            return
        if self._id3 is not None:
            self._id3.add(
                COMM(
                    encoding=3,
                    lang="eng",
                    desc="",
                    text=self.ctx.track_info.tags.comment or "",
                )
            )

    def _embed_cover(self, data: bytes) -> None:
        if self._id3 is not None:
            self._id3.add(
                APIC(
                    encoding=3,
                    mime="image/jpeg",
                    type=3,
                    desc="Cover",
                    data=data,
                )
            )

    def _save(self) -> None:
        # Save EasyID3 tags first
        if isinstance(self._easy, EasyMP3):
            self._easy.save(str(self.ctx.file_path), v1=2, v2_version=3, v23_sep=None)
        # Then save ID3 tags (APIC, USLT, etc.)
        if self._id3 is not None:
            self._id3.save(str(self.ctx.file_path), v1=2, v2_version=3, v23_sep=None)

    def _set_replay_gain(self) -> None:
        """MP3 does not support replay gain in the same way."""
        pass

    def tag(self) -> None:
        """Executes the full tagging process for MP3."""
        super().tag()
        self._set_comment()


class M4ATagger(BaseTagger):
    """Tagger implementation for M4A files."""

    def _create_tagger(self) -> EasyMP4:
        tagger = EasyMP4(str(self.ctx.file_path))

        # Register custom keys
        tagger.RegisterTextKey("isrc", "----:com.apple.itunes:ISRC")
        tagger.RegisterTextKey("upc", "----:com.apple.itunes:UPC")
        if self.ctx.track_info.explicit is not None:
            tagger.RegisterTextKey("explicit", "rtng")
        tagger.RegisterTextKey("covr", "covr")
        if self.ctx.embedded_lyrics:
            tagger.RegisterTextKey("lyrics", "\xa9lyr")

        self._clean_dash_tags(tagger.tags)
        return tagger

    def _set_track_disc_numbers(self) -> None:
        self._set_track_disc_numbers_combined()

    def _set_explicit(self) -> None:
        if self.ctx.track_info.explicit is not None:
            self.tagger["explicit"] = (
                b"\x01" if self.ctx.track_info.explicit else b"\x02"
            )

    def _set_isrc_upc(self) -> None:
        tags = self.ctx.track_info.tags
        if tags.isrc:
            self.tagger["isrc"] = tags.isrc.encode()
        if tags.upc:
            self.tagger["upc"] = tags.upc.encode()

    def _set_label(self) -> None:
        if not self.ctx.track_info.tags.label:
            return
        tagger = self.tagger
        if not isinstance(tagger, EasyMP4):
            return
        tagger.RegisterTextKey("label", "\xa9pub")
        tagger["label"] = self.ctx.track_info.tags.label

    def _set_description(self) -> None:
        """Sets description tag for M4A."""
        if not self.ctx.track_info.tags.description:
            return
        tagger = self.tagger
        if not isinstance(tagger, EasyMP4):
            return
        tagger.RegisterTextKey("desc", "description")
        tagger["description"] = self.ctx.track_info.tags.description

    def _set_comment(self) -> None:
        """Sets comment tag for M4A."""
        if not self.ctx.track_info.tags.comment:
            return
        tagger = self.tagger
        if not isinstance(tagger, EasyMP4):
            return
        tagger.RegisterTextKey("comment", "\xa9cmt")
        tagger["comment"] = self.ctx.track_info.tags.comment

    def _set_credits(self) -> None:
        if not self.ctx.credits_list:
            return
        tagger = self.tagger
        if not isinstance(tagger, EasyMP4):
            return

        for credit in self.ctx.credits_list:
            tagger.RegisterTextKey(credit.type, f"----:com.apple.itunes:{credit.type}")
            tagger[credit.type] = [name.encode() for name in credit.names]

    def _set_lyrics(self) -> None:
        if self.ctx.embedded_lyrics:
            self.tagger["lyrics"] = self.ctx.embedded_lyrics

    def _set_extra_tags(self) -> None:
        """Sets extra custom tags for M4A."""
        tagger = self.tagger
        if not isinstance(tagger, EasyMP4):
            return

        for key, value in self.ctx.track_info.tags.extra_tags.items():
            tagger.RegisterTextKey(key, f"----:com.apple.itunes:{key}")
            tagger[key] = str(value).encode()

    def _embed_cover(self, data: bytes) -> None:
        self.tagger["covr"] = [MP4Cover(data, imageformat=MP4Cover.FORMAT_JPEG)]

    def _save(self) -> None:
        self.tagger.save()

    def _set_replay_gain(self) -> None:
        """M4A does not support replay gain in the same way."""
        pass

    def tag(self) -> None:
        """Executes the full tagging process for M4A."""
        self._set_description()
        self._set_comment()
        self._set_extra_tags()
        super().tag()


def create_tagger(ctx: TaggingContext) -> BaseTagger:
    """Factory function to create the appropriate tagger for a container.

    Args:
        ctx: Tagging context containing file info and metadata.

    Returns:
        A container-specific tagger instance.

    Raises:
        ValueError: If the container format is not supported.
    """
    tagger_map: dict[ContainerEnum, type[BaseTagger]] = {
        ContainerEnum.flac: FLACTagger,
        ContainerEnum.ogg: OggVorbisTagger,
        ContainerEnum.opus: OpusTagger,
        ContainerEnum.mp3: MP3Tagger,
        ContainerEnum.m4a: M4ATagger,
    }

    tagger_class = tagger_map.get(ctx.container)
    if tagger_class is None:
        raise ValueError(f"Unsupported container format: {ctx.container}")

    return tagger_class(ctx)


def tag_file(ctx: TaggingContext) -> None:
    """Tags an audio file with metadata.

    This is the main entry point for tagging audio files. It creates the
    appropriate tagger based on the container format and applies all metadata.

    Args:
        ctx: Tagging context containing file path, cover, track info,
            credits, lyrics, and container format.

    Raises:
        TagSavingFailure: If tagging fails.
        ValueError: If the container format is not supported.
    """
    tagger = create_tagger(ctx)
    tagger.tag()
