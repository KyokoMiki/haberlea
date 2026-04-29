"""Result objects for the download pipeline.

Typed structs replacing tuple return values, keeping only those
that carry 3+ fields or are consumed by multiple call-sites.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import msgspec

if TYPE_CHECKING:
    from pathlib import Path

    from haberlea.downloader.contexts import TrackContext
    from haberlea.plugins.base import ModuleBase
    from haberlea.utils.models import (
        CodecEnum,
        ContainerEnum,
    )
    from haberlea.utils.progress import ProgressStatus


class TrackFileResult(msgspec.Struct, frozen=True):
    """Audio file download result.

    Attributes:
        path: Downloaded file path, or None on failure.
        codec: Audio codec used.
        container: Container format used.
    """

    path: Path | None
    codec: CodecEnum
    container: ContainerEnum


class LyricsResult(msgspec.Struct, frozen=True):
    """Lyrics retrieval result.

    Attributes:
        embedded: Lyrics text for tag embedding.
        synced: Synced (LRC) lyrics, or None if unavailable.
    """

    embedded: str
    synced: str | None


class FailedTrack(msgspec.Struct, frozen=True):
    """Failed track info.

    Attributes:
        track_id: The track identifier.
        reason: Human-readable failure reason.
    """

    track_id: str
    reason: str


class DownloadSummary(msgspec.Struct, frozen=True):
    """Download session summary.

    Attributes:
        completed: List of completed track IDs.
        failed: List of failed tracks with reasons.
    """

    completed: list[str]
    failed: list[FailedTrack]


class ModuleWithAccount(msgspec.Struct, frozen=True):
    """Module loading result with account index.

    Attributes:
        module: The loaded module instance.
        account_index: Which account was used.
    """

    module: ModuleBase
    account_index: int


class TrackDownloadOutput(msgspec.Struct, frozen=True):
    """Full output from TrackDownloader.download().

    Bundles download status with the TrackContext and file result
    so the facade can pass them downstream without re-deriving.

    Attributes:
        path: Final file path, or None if skipped/failed.
        status: Download outcome status.
        ctx: Track context (None when skipped before context creation).
        file_result: Audio file result (None when skipped).
    """

    path: Path | None
    status: ProgressStatus
    ctx: TrackContext | None = None
    file_result: TrackFileResult | None = None
