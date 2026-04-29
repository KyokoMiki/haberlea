"""M3U playlist file utilities.

This module provides functionality for creating and managing M3U playlist files
with support for extended M3U format and concurrent write safety.
"""

import os
from pathlib import Path
from typing import ClassVar

import anyio

from .models import TrackInfo


class M3UPlaylistWriter:
    """Handles M3U playlist file operations with concurrent write safety.

    This class provides methods to create and append to M3U playlist files,
    supporting both simple and extended M3U formats. It uses file-based locking
    to ensure safe concurrent writes from multiple async tasks.

    Attributes:
        extended: Whether to use extended M3U format with #EXTINF tags.
    """

    # Class-level lock registry for concurrent access to the same playlist
    _locks: ClassVar[dict[str, anyio.Lock]] = {}
    _locks_lock: ClassVar[anyio.Lock] = anyio.Lock()

    def __init__(self, extended: bool) -> None:
        """Initialize playlist writer.

        Args:
            extended: Whether to use extended M3U format with #EXTINF metadata.
        """
        self.extended = extended

    @classmethod
    async def _get_lock(cls, playlist_path: Path) -> anyio.Lock:
        """Get or create a lock for a specific playlist file.

        Args:
            playlist_path: Path to the playlist file.

        Returns:
            An anyio.Lock for the specified playlist.
        """
        key = str(playlist_path)
        async with cls._locks_lock:
            if key not in cls._locks:
                cls._locks[key] = anyio.Lock()
            return cls._locks[key]

    async def create(self, playlist_path: Path) -> None:
        """Create empty playlist file with optional header.

        Creates a new M3U playlist file. If extended format is enabled,
        writes the #EXTM3U header.

        Args:
            playlist_path: Path to the playlist file.
        """
        lock = await self._get_lock(playlist_path)
        async with lock:
            content = "#EXTM3U\n" if self.extended else ""
            await anyio.Path(playlist_path).write_text(content, encoding="utf-8")

    async def add_track(
        self,
        playlist_path: Path,
        track_info: TrackInfo,
        track_location: Path,
    ) -> None:
        """Add a track entry to the playlist.

        Appends a track entry to an existing playlist file. In extended format,
        includes #EXTINF metadata with duration and artist/title information.

        Args:
            playlist_path: Path to the playlist file.
            track_info: Track information containing metadata.
            track_location: Path to the track file.
        """
        # Build relative track path
        track_path = self._build_track_path(playlist_path, track_location)

        # Build entry lines
        lines: list[str] = []
        if self.extended:
            duration = track_info.duration or -1
            artist = track_info.artists[0] if track_info.artists else "Unknown"
            lines.append(f"#EXTINF:{duration},{artist} - {track_info.name}")
        lines.append(track_path)

        # Write with lock to ensure concurrent safety
        lock = await self._get_lock(playlist_path)
        async with (
            lock,
            await anyio.open_file(str(playlist_path), "a", encoding="utf-8") as f,
        ):
            await f.write("\n".join(lines) + "\n")

    def _build_track_path(self, playlist_path: Path, track_location: Path) -> str:
        """Build the relative track path for a playlist entry.

        Args:
            playlist_path: Path to the playlist file.
            track_location: Path to the track file.

        Returns:
            The formatted relative track path for the playlist entry.
        """
        # Path.relative_to() only works for subpaths,
        # so we use os.path.relpath which handles arbitrary paths
        return os.path.relpath(str(track_location), str(playlist_path.parent))

    @classmethod
    async def cleanup_locks(cls) -> None:
        """Clean up unused locks from the registry.

        Call this periodically or after batch operations to free memory
        from locks that are no longer needed.
        """
        async with cls._locks_lock:
            cls._locks.clear()
