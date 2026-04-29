"""Download orchestration — coordinates modules, queue, and extensions."""

import logging
from typing import TYPE_CHECKING, Any

import anyio

from haberlea.download_queue import DownloadJob, DownloadQueue
from haberlea.downloader.contexts import (
    AlbumQueueRequest,
    ArtistQueueRequest,
    DownloadRequest,
    ModuleRef,
    PlaylistQueueRequest,
    QueueingContext,
    TrackQueueRequest,
)
from haberlea.downloader.facade import Downloader
from haberlea.downloader.results import DownloadSummary
from haberlea.utils.exceptions import (
    InvalidInput,
    InvalidModuleError,
    ModuleDoesNotSupportAbility,
    RegionRestrictedError,
)
from haberlea.utils.models import (
    CodecOptions,
    DownloadTypeEnum,
    MediaIdentification,
    ModuleModes,
    QualityEnum,
)
from haberlea.utils.settings import settings

if TYPE_CHECKING:
    from anyio.abc import TaskGroup

    from .haberlea import Haberlea

logger = logging.getLogger(__name__)


def find_preferred_account_index(
    accounts: list[dict[str, Any]], url_region: str
) -> int:
    """Finds the account index whose region matches the URL region.

    Performs case-insensitive exact matching of the account ``region``
    field against the region extracted from the download URL.

    Args:
        accounts: List of account configuration dicts.
        url_region: Region string extracted from URL (may be empty).

    Returns:
        Index of the matching account, or 0 if no match.
    """
    if not url_region:
        return 0

    url_region_lower = url_region.lower()
    for idx, account in enumerate(accounts):
        account_region = account.get("region", "")
        if not account_region:
            continue
        if account_region.lower() == url_region_lower:
            return idx

    return 0


async def haberlea_core_download(request: DownloadRequest) -> DownloadSummary:
    """Orchestrates media downloads using a global concurrent queue.

    Args:
        request: Download orchestration request bundling all parameters.

    Returns:
        DownloadSummary with completed track IDs and failed track details.

    Raises:
        ModuleDoesNotSupportAbility: If a module lacks required capability.
        InvalidModuleError: If a specified module doesn't exist.
        InvalidInput: If an unknown media type is encountered.
    """
    gs = settings.global_settings
    haberlea_session = request.session

    extension_tasks: TaskGroup | None = None

    async def on_job_complete(job: DownloadJob) -> None:
        """Spawns background task to run extensions when job completes."""
        if extension_tasks is not None and job.definition.download_path:
            extension_tasks.start_soon(
                haberlea_session.extension_manager.run_for_job, job
            )

    module_settings = haberlea_session.module_registry.state.module_settings
    module_limits: dict[str, int] = {
        name: info.max_concurrent_downloads
        for name, info in module_settings.items()
        if info.max_concurrent_downloads is not None
        and info.max_concurrent_downloads > 0
    }

    queue = DownloadQueue(
        max_concurrent=gs.runtime.concurrent_downloads,
        quality_tier=QualityEnum[gs.quality.tier.upper()],
        codec_options=CodecOptions(
            spatial_codecs=gs.quality.spatial_codecs,
            proprietary_codecs=gs.quality.proprietary_codecs,
        ),
        on_job_complete=on_job_complete,
        module_limits=module_limits,
    )

    await _validate_third_party_modules(haberlea_session, request.third_party_modules)

    downloader = Downloader(
        modules=haberlea_session.module_provider,
        extensions=[
            ext.instance for ext in haberlea_session.extension_manager.extensions
        ],
        path=request.output_path,
        queue=queue,
        third_party_modules=request.third_party_modules,
    )

    summary = DownloadSummary(completed=[], failed=[])

    logger.info("=== Collecting tracks ===")
    for module_name, items in request.media_to_download.items():
        ctx = QueueingContext(
            session=haberlea_session,
            downloader=downloader,
            separate_download_module=request.separate_download_module,
        )
        await _queue_module_items(ctx, module_name, items)

    logger.info("Total tracks queued: %d", queue.track_count)

    if request.on_queue_ready:
        request.on_queue_ready(queue)

    async with anyio.create_task_group() as tg:
        extension_tasks = tg
        summary = await downloader.process_queue()

    await haberlea_session.extension_manager.run_finalize()

    return summary


async def _queue_module_items(
    ctx: QueueingContext,
    module_name: str,
    items: list[MediaIdentification],
) -> None:
    """Queues all media items from a module.

    Selects the preferred account based on the URL region hint from the
    first item, then queues all items using that account.

    Args:
        ctx: Queueing context with session, downloader, and separate_download_module.
        module_name: The module name.
        items: List of media items to queue.
    """
    supported_modes = ctx.session.module_registry.state.module_settings[
        module_name
    ].module_supported_modes
    if ModuleModes.download not in supported_modes:
        raise ModuleDoesNotSupportAbility(module_name, "track downloading")

    # Select preferred account based on URL region
    accounts = ctx.session.get_module_accounts(module_name)
    url_region = items[0].url_region if items else ""
    preferred_index = find_preferred_account_index(accounts, url_region)

    module = await ctx.session.load_module(module_name, preferred_index)
    module_ref = ModuleRef(name=module_name, instance=module)

    for media in items:
        await _queue_media_item(ctx, module_ref, media, account_index=preferred_index)


async def _queue_media_item(
    ctx: QueueingContext,
    module_ref: ModuleRef,
    media: MediaIdentification,
    account_index: int = 0,
) -> None:
    """Queues a single media item with multi-account fallback.

    Args:
        ctx: Queueing context with session, downloader, and separate_download_module.
        module_ref: Module name and loaded instance.
        media: The media item to queue.
        account_index: Current account index being used.
    """
    media_type = media.media_type
    media_id = media.media_id
    module_name = module_ref.name

    custom_module_ref = None

    if (
        ctx.separate_download_module != "default"
        and ctx.separate_download_module != module_name
        and media_type is DownloadTypeEnum.playlist
    ):
        custom_module = await ctx.session.load_module(ctx.separate_download_module)
        custom_module_ref = ModuleRef(
            name=ctx.separate_download_module, instance=custom_module
        )

    try:
        match media_type:
            case DownloadTypeEnum.album:
                await ctx.downloader.queue_album(
                    AlbumQueueRequest(
                        module=module_ref,
                        album_id=media_id,
                        original_url=media.original_url,
                    )
                )
            case DownloadTypeEnum.track:
                await ctx.downloader.queue_track(
                    TrackQueueRequest(
                        module=module_ref,
                        track_id=media_id,
                        original_url=media.original_url,
                    )
                )
            case DownloadTypeEnum.playlist:
                await ctx.downloader.queue_playlist(
                    PlaylistQueueRequest(
                        module=module_ref,
                        playlist_id=media_id,
                        original_url=media.original_url,
                        custom_module=custom_module_ref,
                    )
                )
            case DownloadTypeEnum.artist:
                await ctx.downloader.queue_artist(
                    ArtistQueueRequest(
                        module=module_ref,
                        artist_id=media_id,
                        original_url=media.original_url,
                    )
                )
            case _:
                raise InvalidInput(
                    f'Unknown media type "{media_type}"',
                    field="media_type",
                    value=media_type,
                )
    except RegionRestrictedError:
        account_count = ctx.session.module_registry.get_module_account_count(
            module_name
        )
        next_account = _find_next_account(account_index, account_count)

        if next_account is None:
            raise

        logger.warning(
            "Region restricted for %s %s with account %d, trying account %d",
            media_type.value,
            media_id,
            account_index,
            next_account,
        )
        logger.warning(
            "Account %d region restricted for %s %s, switching to account %d...",
            account_index,
            media_type.value,
            media_id,
            next_account,
        )

        new_module = await ctx.session.load_module(module_name, next_account)
        new_module_ref = ModuleRef(name=module_name, instance=new_module)
        await _queue_media_item(ctx, new_module_ref, media, account_index=next_account)


def _find_next_account(current: int, total: int) -> int | None:
    """Finds the next account index to try.

    Args:
        current: Current account index.
        total: Total number of accounts.

    Returns:
        Next account index, or None if no more accounts.
    """
    next_idx = current + 1
    if next_idx < total:
        return next_idx
    return None


async def _validate_third_party_modules(
    session: "Haberlea",
    third_party_modules: dict[ModuleModes, str],
) -> None:
    """Validates and loads third-party modules.

    Args:
        session: The Haberlea session.
        third_party_modules: Module selections to validate.

    Raises:
        InvalidModuleError: If a module doesn't exist.
        ModuleDoesNotSupportAbility: If a module lacks required capability.
    """
    for mode, module_name in third_party_modules.items():
        if module_name not in session.module_registry.state.module_list:
            raise InvalidModuleError(module_name)

        module_modes = session.module_registry.state.module_settings[
            module_name
        ].module_supported_modes
        if isinstance(mode, ModuleModes) and mode not in module_modes:
            raise ModuleDoesNotSupportAbility(module_name, str(mode))

        await session.load_module(module_name)


async def cleanup_modules(session: "Haberlea") -> None:
    """Closes all loaded modules to release resources.

    Intended to be called once at process shutdown (CLI exit / WebUI
    lifespan shutdown), not between individual download batches — WebUI
    reuses module instances across requests to keep aiohttp sessions alive.

    Args:
        session: The Haberlea session.
    """
    loaded = session.module_registry.state.loaded_modules
    for module_name, module_instance in loaded.items():
        try:
            await module_instance.close()
        except Exception:
            logger.debug("Error closing module %s", module_name)
