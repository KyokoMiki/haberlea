"""Path building utilities for music downloads.

This module provides path construction functionality for organizing
downloaded music files into appropriate directory structures.
"""

import unicodedata
from pathlib import Path
from typing import Any

from .models import AlbumInfo, DownloadTypeEnum, PlaylistInfo, TrackInfo
from .settings import FormattingSettings
from .utils import fix_byte_limit, sanitise_name


def get_artist_initials(artist_name: str) -> str:
    """Extract artist initials for folder organization.

    Args:
        artist_name: The artist name.

    Returns:
        Single character initial (uppercase letter or '#').
    """
    initial = artist_name.lower()
    if initial.startswith("the "):
        initial = initial[4:]

    if not initial:
        return "#"

    # Normalize unicode characters
    initial = (
        unicodedata.normalize("NFKD", initial[0])
        .encode("ascii", "ignore")
        .decode("utf-8")
    )

    return initial.upper() if initial.isalpha() else "#"


def _sanitise_int(
    value: int | None,
    zfill_enabled: bool,
    zfill_number: int,
) -> str | None:
    """Sanitise an optional integer field with optional zero-fill.

    Args:
        value: The integer value, or None.
        zfill_enabled: Whether to zero-fill.
        zfill_number: Number of digits for zero-filling.

    Returns:
        Sanitised string, or None if value is None.
    """
    if value is None:
        return None
    s = sanitise_name(str(value))
    return s.zfill(zfill_number) if zfill_enabled else s


def build_track_tags(
    track_info: TrackInfo,
    zfill_enabled: bool,
    zfill_number: int,
) -> dict[str, Any]:
    """Build sanitized track tags for path formatting.

    Args:
        track_info: Track information.
        zfill_enabled: Whether to zero-fill numeric fields.
        zfill_number: Number of digits for zero-filling.

    Returns:
        Dictionary of sanitized tags.
    """
    tags = track_info.tags
    zf = zfill_enabled
    zn = zfill_number

    return {
        # TrackInfo fields
        "name": sanitise_name(track_info.name),
        "album": sanitise_name(track_info.album),
        "album_id": sanitise_name(track_info.album_id),
        "artist": (
            sanitise_name(track_info.artists[0])
            if track_info.artists
            else "Unknown Artist"
        ),
        "artists": sanitise_name(", ".join(track_info.artists)),
        "codec": sanitise_name(str(track_info.codec)),
        "release_year": sanitise_name(str(track_info.release_year)),
        "duration": (
            sanitise_name(str(track_info.duration)) if track_info.duration else None
        ),
        "explicit": " [E]" if track_info.explicit else "",
        "bit_depth": sanitise_name(str(track_info.bit_depth)),
        "sample_rate": sanitise_name(str(track_info.sample_rate)),
        "bitrate": (
            sanitise_name(str(track_info.bitrate)) if track_info.bitrate else None
        ),
        # Tags fields
        "album_artist": (
            sanitise_name(tags.album_artist) if tags.album_artist else None
        ),
        "composer": sanitise_name(tags.composer) if tags.composer else None,
        "track_number": _sanitise_int(tags.track_number, zf, zn),
        "total_tracks": _sanitise_int(tags.total_tracks, zf, zn),
        "disc_number": _sanitise_int(tags.disc_number, zf, zn),
        "total_discs": _sanitise_int(tags.total_discs, zf, zn),
        "copyright": sanitise_name(tags.copyright) if tags.copyright else None,
        "isrc": sanitise_name(tags.isrc) if tags.isrc else None,
        "upc": sanitise_name(tags.upc) if tags.upc else None,
        "release_date": (
            sanitise_name(tags.release_date) if tags.release_date else None
        ),
        "description": (sanitise_name(tags.description) if tags.description else None),
        "comment": sanitise_name(tags.comment) if tags.comment else None,
        "label": sanitise_name(tags.label) if tags.label else None,
        "genres": (sanitise_name(", ".join(tags.genres)) if tags.genres else None),
    }


class PathBuilder:
    """Handles path construction for downloads."""

    def __init__(self, base_path: Path, formatting: FormattingSettings) -> None:
        """Initialize path builder.

        Args:
            base_path: Base download path.
            formatting: Path formatting configuration.
        """
        self.base_path = base_path
        self._formatting = formatting

    def build_album_path(self, album_id: str, album_info: AlbumInfo) -> Path:
        """Build album directory path.

        Args:
            album_id: Album identifier.
            album_info: Album information.

        Returns:
            Full album directory path.
        """
        album_tags: dict[str, Any] = {
            "name": sanitise_name(album_info.name),
            "artist": sanitise_name(album_info.artist),
            "release_year": sanitise_name(str(album_info.release_year)),
            "id": str(album_id),
            "quality": f" [{album_info.quality}]" if album_info.quality else "",
            "explicit": " [E]" if album_info.explicit else "",
            "artist_initials": get_artist_initials(album_info.artist),
            "duration": (
                sanitise_name(str(album_info.duration)) if album_info.duration else None
            ),
            "upc": (sanitise_name(album_info.upc) if album_info.upc else None),
            "description": (
                sanitise_name(album_info.description)
                if album_info.description
                else None
            ),
        }

        try:
            album_path = self.base_path / self._formatting.album_format.format(
                **album_tags
            )
        except (KeyError, ValueError):
            # Fallback to safe default format if user format has missing keys
            album_path = (
                self.base_path
                / album_tags["artist"]
                / f"{album_tags['name']}{album_tags['explicit']}"
            )
        album_path = fix_byte_limit(album_path)

        return album_path

    def build_playlist_path(self, playlist_info: PlaylistInfo) -> Path:
        """Build playlist directory path.

        Args:
            playlist_info: Playlist information.

        Returns:
            Full playlist directory path.
        """
        playlist_tags: dict[str, Any] = {
            "name": sanitise_name(playlist_info.name),
            "creator": sanitise_name(playlist_info.creator),
            "release_year": sanitise_name(str(playlist_info.release_year)),
            "explicit": " [E]" if playlist_info.explicit else "",
            "duration": (
                sanitise_name(str(playlist_info.duration))
                if playlist_info.duration
                else None
            ),
            "description": (
                sanitise_name(playlist_info.description)
                if playlist_info.description
                else None
            ),
        }

        try:
            playlist_path = self.base_path / self._formatting.playlist_format.format(
                **playlist_tags
            )
        except (KeyError, ValueError):
            # Fallback to safe default format
            playlist_path = (
                self.base_path / f"{playlist_tags['name']}{playlist_tags['explicit']}"
            )
        playlist_path = fix_byte_limit(playlist_path)

        return playlist_path

    def build_track_path(
        self,
        track_info: TrackInfo,
        album_location: Path,
        download_mode: DownloadTypeEnum,
    ) -> Path:
        """Build track file path (without extension).

        Args:
            track_info: Track information.
            album_location: Album directory path.
            download_mode: Current download mode.

        Returns:
            Full track file path without extension.
        """
        zfill_number = (
            len(str(track_info.tags.total_tracks))
            if download_mode is not DownloadTypeEnum.track
            else 1
        )
        track_tags = build_track_tags(
            track_info,
            self._formatting.enable_zfill,
            zfill_number,
        )

        if (
            download_mode is DownloadTypeEnum.track
            and not self._formatting.force_album_format
        ):
            try:
                single_fmt = self._formatting.single_full_path_format
                track_location_name = self.base_path / single_fmt.format(**track_tags)
            except (KeyError, ValueError):
                # Fallback to safe default format
                track_location_name = (
                    self.base_path
                    / track_tags["artist"]
                    / f"{track_tags['name']}{track_tags['explicit']}"
                )
        elif (
            track_info.tags.total_tracks == 1
            and not self._formatting.force_album_format
        ):
            try:
                single_fmt = self._formatting.single_full_path_format
                track_location_name = album_location / single_fmt.format(**track_tags)
            except (KeyError, ValueError):
                # Fallback to safe default format
                track_location_name = (
                    album_location / f"{track_tags['name']}{track_tags['explicit']}"
                )
        else:
            location = album_location
            disc_prefix = ""
            if track_info.tags.total_discs and track_info.tags.total_discs > 1:
                disc_prefix = f"{track_info.tags.disc_number}-"
            try:
                filename = self._formatting.track_filename_format.format(**track_tags)
                track_location_name = location / (disc_prefix + filename)
            except (KeyError, ValueError):
                # Fallback to safe default format
                track_location_name = (
                    location / f"{track_tags['track_number']} - "
                    f"{track_tags['name']}{track_tags['explicit']}"
                )

        track_location_name = fix_byte_limit(track_location_name)

        return track_location_name
