"""Downloader package — types, contexts, and result objects.

This package provides the shared type definitions used across the
download pipeline.
"""

from .contexts import (
    AlbumQueueRequest,
    ArtistQueueRequest,
    DownloadRequest,
    LoginContext,
    ModuleRef,
    PlaylistQueueRequest,
    TrackContext,
    TrackQueueRequest,
)
from .results import (
    DownloadSummary,
    FailedTrack,
    LyricsResult,
    ModuleWithAccount,
    TrackDownloadOutput,
    TrackFileResult,
)

__all__ = [
    # Results
    "DownloadSummary",
    "FailedTrack",
    "LyricsResult",
    "ModuleWithAccount",
    "TrackDownloadOutput",
    "TrackFileResult",
    # Contexts
    "AlbumQueueRequest",
    "ArtistQueueRequest",
    "DownloadRequest",
    "LoginContext",
    "ModuleRef",
    "PlaylistQueueRequest",
    "TrackContext",
    "TrackQueueRequest",
]
