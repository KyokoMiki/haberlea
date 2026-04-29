"""Track finalizer — post-download processing pipeline.

Single responsibility: tag, move, and M3U write.
No network I/O, no metadata fetching.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import anyio
import msgspec

from haberlea.tagging import TaggingContext, tag_file
from haberlea.utils.exceptions import TagSavingFailure
from haberlea.utils.m3u import M3UPlaylistWriter
from haberlea.utils.utils import move_file, sanitise_name

if TYPE_CHECKING:
    from pathlib import Path

    from haberlea.downloader.contexts import TrackContext
    from haberlea.downloader.results import LyricsResult, TrackFileResult
    from haberlea.utils.models import ContainerEnum, CreditsInfo, TrackInfo
    from haberlea.utils.settings import CoversSettings, LyricsSettings, PlaylistSettings
    from haberlea.utils.tempfile_manager import TempFileManager

logger = logging.getLogger(__name__)


class TrackMetadata(msgspec.Struct, frozen=True):
    """Supplementary metadata for tagging.

    Bundles cover_path, lyrics, and credits so _tag() has 3 params not 5.
    """

    cover_path: Path | None
    lyrics: LyricsResult
    credits: list[CreditsInfo]


class PlaylistContext(msgspec.Struct, frozen=True):
    """Optional playlist context for M3U writing."""

    download_path: Path
    name: str


class TrackFinalizer:
    """Finalizes downloaded tracks: tag, move, M3U.

    Single responsibility: post-download processing pipeline.
    """

    def __init__(
        self,
        temp: TempFileManager,
        covers: CoversSettings,
        lyrics: LyricsSettings,
        playlist: PlaylistSettings,
    ) -> None:
        """Initialize the track finalizer.

        Args:
            temp: Temporary file manager.
            covers: Cover art settings (embed_cover is consumed).
            lyrics: Lyrics settings (save_synced_lyrics is consumed).
            playlist: M3U playlist settings.
        """
        self._temp = temp
        self._covers = covers
        self._lyrics = lyrics
        self._m3u_config = playlist

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def finalize(
        self,
        ctx: TrackContext,
        file_result: TrackFileResult,
        metadata: TrackMetadata,
        playlist: PlaylistContext | None = None,
    ) -> None:
        """Runs the full post-download pipeline.

        Args:
            ctx: Track context with track_info, location_name, codec, container.
            file_result: Audio file download result with current file path.
            metadata: Supplementary metadata (cover, lyrics, credits).
            playlist: Optional playlist context for M3U writing.
        """
        track_location = file_result.path
        if track_location is None:
            raise ValueError("Cannot finalize track with no file path")

        container = file_result.container

        # Save synced lyrics if enabled
        if metadata.lyrics.synced and self._lyrics.save_synced_lyrics:
            lrc_location = ctx.location_name.parent / (ctx.location_name.name + ".lrc")
            if not lrc_location.is_file():
                await anyio.Path(lrc_location).write_text(
                    metadata.lyrics.synced, encoding="utf-8"
                )

        # Compute final location (used for M3U entry and move target)
        final_location = ctx.location_name.parent / (
            ctx.location_name.name + f".{container.name}"
        )

        # Add to M3U playlist (use final location, not temp path)
        if playlist is not None:
            await self._add_to_m3u(playlist, ctx.track_info, final_location)

        # Tag file
        self._tag_with_info(track_location, ctx.track_info, metadata, container)

        # Move to final location
        await move_file(track_location, final_location)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _tag_with_info(
        self,
        track_location: Path,
        track_info: TrackInfo,
        metadata: TrackMetadata,
        container: ContainerEnum,
    ) -> None:
        """Writes metadata tags including full TrackInfo.

        Args:
            track_location: Path to the audio file.
            track_info: Full track metadata.
            metadata: Supplementary metadata.
            container: Audio container format.
        """
        try:
            tag_file(
                TaggingContext(
                    file_path=track_location,
                    image_path=metadata.cover_path
                    if self._covers.embed_cover
                    else None,
                    track_info=track_info,
                    credits_list=metadata.credits,
                    embedded_lyrics=metadata.lyrics.embedded,
                    container=container,
                )
            )
        except TagSavingFailure:
            logger.warning("Tagging failed for %s", track_location)

    async def _add_to_m3u(
        self,
        playlist: PlaylistContext,
        track_info: TrackInfo,
        track_location: Path,
    ) -> None:
        """Add track to M3U playlist, creating it if needed.

        Args:
            playlist: Playlist context with download_path and name.
            track_info: Track metadata.
            track_location: Path to the track file.
        """
        if not self._m3u_config.save_m3u:
            return

        m3u_path = playlist.download_path / f"{sanitise_name(playlist.name)}.m3u8"

        if not m3u_path.exists():
            playlist.download_path.mkdir(parents=True, exist_ok=True)
            writer = M3UPlaylistWriter(extended=self._m3u_config.extended_m3u)
            await writer.create(m3u_path)

        writer = M3UPlaylistWriter(extended=self._m3u_config.extended_m3u)
        await writer.add_track(m3u_path, track_info, track_location)
