"""Asset manager — cover art, lyrics, and credits fetching.

Single responsibility: fetch supplementary track assets.
No audio file I/O, no tagging.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import anyio

from haberlea.downloader.contexts import build_artwork_settings
from haberlea.downloader.results import LyricsResult
from haberlea.utils.models import (
    CoverCompressionEnum,
    CoverOptions,
    CreditsInfo,
    DownloadTypeEnum,
    ImageFileTypeEnum,
    LyricsInfo,
    ModuleModes,
)
from haberlea.utils.utils import (
    _process_artwork,
    compare_images,
    download_file,
    get_image_resolution,
    move_file,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from pathlib import Path

    from haberlea.downloader.contexts import TrackContext
    from haberlea.downloader.protocols import ModuleProvider
    from haberlea.plugins.base import ModuleBase
    from haberlea.utils.models import SearchResult, TrackInfo
    from haberlea.utils.settings import CoversSettings, LyricsSettings
    from haberlea.utils.tempfile_manager import TempFileManager

logger = logging.getLogger(__name__)


class CoverCache:
    """Album-level cover art cache with its own lock.

    Extracted so AssetManager doesn't hold raw dict + lock as separate fields.
    Uses per-album events to ensure each cover is downloaded only once,
    even under concurrent access.
    """

    def __init__(self) -> None:
        self._cache: dict[str, Path | None] = {}
        self._pending: dict[str, anyio.Event] = {}
        self._lock = anyio.Lock()

    async def get_or_fetch(
        self,
        album_id: str,
        fetch: Callable[[], Coroutine[Any, Any, Path | None]],
    ) -> Path | None:
        """Return cached cover or call fetch() and cache the result.

        Only the first caller for a given album_id triggers the download;
        concurrent callers wait on an event and reuse the result.

        Args:
            album_id: Cache key.
            fetch: Async callable that downloads the cover.

        Returns:
            Path to cover file, or None if fetch failed.
        """
        async with self._lock:
            if album_id in self._cache:
                return self._cache[album_id]
            if album_id in self._pending:
                event = self._pending[album_id]
            else:
                event = anyio.Event()
                self._pending[album_id] = event
                event = None  # signal: this caller is the fetcher

        if event is not None:
            # Another coroutine is already fetching — wait for it
            await event.wait()
            return self._cache.get(album_id)

        # This coroutine is the fetcher
        try:
            result = await fetch()
        except Exception:
            async with self._lock:
                self._pending.pop(album_id, None)
            raise

        async with self._lock:
            self._cache[album_id] = result
            pending_event = self._pending.pop(album_id, None)

        if pending_event is not None:
            pending_event.set()

        return result


class AssetManager:
    """Fetches cover art, lyrics, and credits for tracks.

    Three fields: config (dep) + modules (dep) + cover_cache (state).
    """

    def __init__(
        self,
        third_party_modules: dict[ModuleModes, str],
        cover_config: CoversSettings,
        lyrics_config: LyricsSettings,
        modules: ModuleProvider,
        temp: TempFileManager,
    ) -> None:
        """Initialize the asset manager.

        Args:
            third_party_modules: Mapping of mode to module name for third-party
                asset fetching (covers/lyrics/credits).
            cover_config: Cover art settings.
            lyrics_config: Lyrics settings.
            modules: Module provider interface.
            temp: Temporary file manager.
        """
        self._third_party_modules = third_party_modules
        self._cover_config = cover_config
        self._lyrics_config = lyrics_config
        self._modules = modules
        self._temp = temp
        self._cover_cache = CoverCache()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def download_cover(self, ctx: TrackContext) -> Path | None:
        """Downloads cover art with album-level caching.

        Args:
            ctx: Track context with track_info, location_name, and module_name.

        Returns:
            Path to the downloaded cover file, or None if download failed.
        """
        track_info = ctx.track_info
        module_name = ctx.task.module_name
        third_party = self._third_party_modules.get(ModuleModes.covers)

        async def fetch() -> Path | None:
            cover_temp = self._temp.get_temp_filename(suffix=".jpg")
            if third_party and third_party != module_name:
                return await self._download_cover_third_party(
                    track_info, ctx.location_name, third_party, cover_temp
                )
            artwork_settings = build_artwork_settings(
                self._modules.get_module_flags(module_name),
                self._cover_config,
            )
            await download_file(
                track_info.cover_url,
                cover_temp,
            )
            _process_artwork(cover_temp, artwork_settings)
            return cover_temp

        cover_url = track_info.cover_url
        if cover_url:
            return await self._cover_cache.get_or_fetch(cover_url, fetch)
        return await fetch()

    async def get_lyrics(self, ctx: TrackContext) -> LyricsResult:
        """Retrieves lyrics for a track.

        Args:
            ctx: Track context with track_id, track_info, module_name, and module.

        Returns:
            LyricsResult with embedded and synced lyrics.
        """
        lc = self._lyrics_config
        if not (lc.embed_lyrics or lc.save_synced_lyrics):
            return LyricsResult(embedded="", synced=None)

        track_info = ctx.track_info
        module_name = ctx.task.module_name
        module = ctx.task.module
        lyrics_info = LyricsInfo()
        third_party = self._third_party_modules.get(ModuleModes.lyrics)

        if third_party and third_party != module_name:
            results = await self._search_by_tags(third_party, track_info)
            if results:
                tp_module = await self._get_module_by_name(third_party)
                lyrics_info = await tp_module.get_track_lyrics(
                    results[0].result_id, data=results[0].data
                )
        elif self._modules.supports_mode(module_name, ModuleModes.lyrics):
            lyrics_info = await module.get_track_lyrics(
                ctx.task.track_id, data=track_info.lyrics_data
            )

        embedded = ""
        if lyrics_info.embedded and lc.embed_lyrics:
            embedded = lyrics_info.embedded
        if lyrics_info.synced and lc.embed_lyrics and lc.embed_synced_lyrics:
            embedded = lyrics_info.synced

        return LyricsResult(embedded=embedded, synced=lyrics_info.synced)

    async def get_credits(self, ctx: TrackContext) -> list[CreditsInfo]:
        """Retrieves credits for a track.

        Args:
            ctx: Track context with track_id, track_info, module_name, and module.

        Returns:
            List of CreditsInfo objects.
        """
        track_info = ctx.track_info
        module_name = ctx.task.module_name
        module = ctx.task.module
        third_party = self._third_party_modules.get(ModuleModes.credits)

        if third_party and third_party != module_name:
            results = await self._search_by_tags(third_party, track_info)
            if results:
                tp_module = await self._get_module_by_name(third_party)
                return await tp_module.get_track_credits(
                    results[0].result_id, data=results[0].data
                )
            return []

        if self._modules.supports_mode(module_name, ModuleModes.credits):
            return await module.get_track_credits(
                ctx.task.track_id, data=track_info.credits_data
            )
        return []

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _download_cover_third_party(
        self,
        track_info: TrackInfo,
        track_location_name: Path,
        module_name: str,
        cover_temp: Path,
    ) -> Path:
        """Download cover using third-party module."""
        default_temp = self._temp.get_temp_filename(suffix=".jpg")
        await download_file(track_info.cover_url, default_temp)

        test_options = CoverOptions(
            file_type=ImageFileTypeEnum.jpg,
            resolution=get_image_resolution(default_temp),
            compression=CoverCompressionEnum.high,
        )

        cover_module = self._modules.state.loaded_modules[module_name]
        rms_threshold = self._cover_config.cover_variance_threshold
        results = await self._search_by_tags(module_name, track_info)

        for result in results:
            test_cover = await cover_module.get_track_cover(
                result.result_id, test_options, data=result.data
            )
            test_temp = self._temp.get_temp_filename(suffix=".jpg")
            await download_file(test_cover.url, test_temp)

            rms = compare_images(default_temp, test_temp)
            await anyio.Path(test_temp).unlink(missing_ok=True)

            if rms < rms_threshold:
                cc = self._cover_config
                jpg_options = CoverOptions(
                    file_type=ImageFileTypeEnum.jpg,
                    resolution=cc.main_resolution,
                    compression=CoverCompressionEnum[cc.main_compression.lower()],
                )
                jpg_cover = await cover_module.get_track_cover(
                    result.result_id, jpg_options, data=result.data
                )
                artwork_settings = build_artwork_settings(
                    self._modules.get_module_flags(module_name),
                    self._cover_config,
                )
                await download_file(
                    jpg_cover.url,
                    cover_temp,
                )
                _process_artwork(cover_temp, artwork_settings)
                await anyio.Path(default_temp).unlink(missing_ok=True)
                return cover_temp

        await move_file(default_temp, cover_temp)
        return cover_temp

    async def _search_by_tags(
        self, module_name: str, track_info: TrackInfo
    ) -> list[SearchResult]:
        """Search for a track by its tags."""
        query = f"{track_info.name} {' '.join(track_info.artists)}"
        module = await self._get_module_by_name(module_name)
        return await module.search(DownloadTypeEnum.track, query, track_info=track_info)

    async def _get_module_by_name(self, module_name: str) -> ModuleBase:
        """Get a module instance by name, loading if necessary."""
        for key, module in self._modules.state.loaded_modules.items():
            if key.startswith(f"{module_name}:"):
                return module
        return await self._modules.load_module(module_name, 0)
