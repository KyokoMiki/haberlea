"""Utility functions for Haberlea.

This module provides common utility functions used throughout the application,
including file operations, HTTP session management, and image processing.
"""

import errno
import hashlib
import logging
import math
import operator
import re
import shutil
import zipfile
from collections.abc import Callable, Sequence
from functools import reduce
from pathlib import Path
from time import gmtime, strftime
from typing import TYPE_CHECKING, Any

import aiohttp
import anyio
import msgspec
from asyncer import asyncify
from PIL import Image, ImageChops
from tenacity import (
    AsyncRetrying,
    before_sleep_log,
    retry_if_exception_type,
    retry_if_not_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .exceptions import InvalidHashTypeError, TemporarySettingsError
from .progress import ProgressMode, advance, get_current_task, reset, update

if TYPE_CHECKING:
    from haberlea.downloader.contexts import ArtworkSettings

logger = logging.getLogger(__name__)


class DownloadConfig(msgspec.Struct, frozen=True):
    """Download behavior configuration.

    Attributes:
        headers: HTTP headers for the request.
        task_id: Task ID for progress reporting.
        chunk_processor: Optional callback to process each chunk before writing.
        chunk_size: Size of chunks to download in bytes.
    """

    headers: dict[str, str] = msgspec.field(default_factory=dict)
    task_id: str | None = None
    chunk_processor: Callable[[bytes, int], bytes] | None = None
    chunk_size: int = 1048576


def hash_string(input_str: str, hash_type: str = "BLAKE2B") -> str:
    """Hashes a string using the specified hash algorithm.

    Args:
        input_str: The string to hash.
        hash_type: The hash algorithm to use. Defaults to "BLAKE2B".
            Supported: "BLAKE2B" (recommended, fast & secure),
                      "MD5" (legacy, platform requirement).

    Returns:
        The hexadecimal digest of the hash.

    Raises:
        InvalidHashTypeError: If an invalid hash type is selected.
    """
    hash_type_upper = hash_type.upper()
    if hash_type_upper == "BLAKE2B":
        return hashlib.blake2b(input_str.encode("utf-8")).hexdigest()
    elif hash_type_upper == "MD5":
        # MD5 is insecure but kept for platform API compatibility (e.g., Qobuz)
        return hashlib.md5(input_str.encode("utf-8")).hexdigest()
    else:
        raise InvalidHashTypeError(hash_type, supported_types=["BLAKE2B", "MD5"])


def create_aiohttp_session(
    timeout: int = 30,
    connector_limit: int = 100,
    read_bufsize: int = 2**20,
) -> aiohttp.ClientSession:
    """Creates an aiohttp ClientSession with connection pool settings.

    Args:
        timeout: Socket read timeout in seconds (time to wait for data chunks).
        connector_limit: Maximum number of concurrent connections.
        read_bufsize: Size of the read buffer in bytes. Defaults to 1 MiB.

    Returns:
        A configured aiohttp ClientSession.
    """
    timeout_config = aiohttp.ClientTimeout(total=None, sock_read=timeout)
    connector = aiohttp.TCPConnector(
        limit=connector_limit,
        ssl=False,
        enable_cleanup_closed=True,
    )
    return aiohttp.ClientSession(
        timeout=timeout_config,
        connector=connector,
        read_bufsize=read_bufsize,
    )


def sanitise_name(name: str | None) -> str:
    """Sanitizes a filename by removing or replacing invalid characters.

    Args:
        name: The filename to sanitize.

    Returns:
        The sanitized filename.
    """
    return (
        re.sub(
            r"[:]",
            "_",
            re.sub(r'[\\/*?",<>|$]', "_", re.sub(r"[ \t]+$", "", str(name).rstrip())),
        )
        if name
        else ""
    )


def fix_byte_limit(path: Path, byte_limit: int = 250) -> Path:
    """Truncates a file path to fit within a byte limit.

    Args:
        path: The file path to truncate.
        byte_limit: Maximum byte size for the filename.

    Returns:
        The truncated file path.
    """
    resolved = path.resolve()
    directory = resolved.parent
    filename = resolved.name
    filename_bytes = filename.encode("utf-8")
    fixed_bytes = filename_bytes[:byte_limit]
    fixed_filename = fixed_bytes.decode("utf-8", "ignore")
    return directory / fixed_filename


def _process_artwork(
    file_location: Path,
    artwork_settings: "ArtworkSettings",
) -> None:
    """Process and resize artwork image.

    Args:
        file_location: Path to the image file.
        artwork_settings: Settings for resizing artwork.
    """
    if not artwork_settings.should_resize:
        return

    new_resolution = artwork_settings.resolution
    new_format = artwork_settings.format
    if new_format == "jpg":
        new_format = "jpeg"

    new_compression: int | None
    if artwork_settings.compression == "low":
        new_compression = 90
    elif artwork_settings.compression == "high":
        new_compression = 70
    else:
        new_compression = 90

    if new_format == "png":
        new_compression = None

    with Image.open(str(file_location)) as im:
        im = im.resize((new_resolution, new_resolution), Image.Resampling.BICUBIC)
        im.save(str(file_location), new_format, quality=new_compression)


async def _download_once(
    url: str,
    file_location: Path,
    session: aiohttp.ClientSession,
    config: DownloadConfig,
) -> None:
    """Download a file in a single attempt.

    Args:
        url: URL to download from.
        file_location: Local file path.
        session: aiohttp session.
        config: Download configuration (headers, task_id, chunk_processor, chunk_size).
    """
    logger.debug("Starting download attempt for: %s", url)
    async with session.get(url, headers=config.headers, ssl=False) as response:
        response.raise_for_status()
        total = response.content_length or 0

        async with await anyio.open_file(str(file_location), "wb") as f:
            chunk_index = 0
            async for chunk in response.content.iter_chunked(config.chunk_size):
                if chunk:
                    original_len = len(chunk)
                    if config.chunk_processor:
                        # Run CPU-intensive decryption in thread pool
                        chunk = await asyncify(config.chunk_processor)(
                            chunk, chunk_index
                        )
                    await f.write(chunk)
                    chunk_index += 1
                    if config.task_id and total > 0:
                        await advance(config.task_id, original_len, total)


async def download_file(
    url: str,
    file_location: Path,
    config: DownloadConfig | None = None,
    session: aiohttp.ClientSession | None = None,
    passthrough_errors: tuple[type[BaseException], ...] = (),
) -> None:
    """Downloads a file asynchronously using aiohttp with automatic retry.

    Progress is automatically reported through the global progress callback.
    If called within a progress context, updates are sent to that task.

    Args:
        url: The URL to download from.
        file_location: The local path to save the file.
        config: Optional download configuration (headers, task_id, chunk_processor,
            chunk_size). Defaults to DownloadConfig() with sensible defaults.
        session: Optional aiohttp session to reuse.
        passthrough_errors: Exception types that bypass retry and are raised
            immediately. Useful when the caller has its own fallback logic
            for specific error classes.

    Raises:
        KeyboardInterrupt: If the download is interrupted by the user.
        aiohttp.ClientError: If the download fails after all retries.
    """
    if config is None:
        config = DownloadConfig()

    if file_location.is_file():
        return

    # Ensure parent directory exists
    file_location.parent.mkdir(parents=True, exist_ok=True)

    close_session = False
    if session is None:
        session = create_aiohttp_session()
        close_session = True

    # Get task ID from context or parameter
    effective_task_id = config.task_id or get_current_task()
    if effective_task_id:
        reset(effective_task_id)
        await update(effective_task_id, mode=ProgressMode.BYTES)

    # Merge effective_task_id into config for _download_with_retry
    effective_config = DownloadConfig(
        headers=config.headers,
        task_id=effective_task_id,
        chunk_processor=config.chunk_processor,
        chunk_size=config.chunk_size,
    )

    try:
        retry_condition = retry_if_exception_type((aiohttp.ClientError, TimeoutError))
        if passthrough_errors:
            retry_condition = retry_condition & retry_if_not_exception_type(
                passthrough_errors
            )

        async for attempt in AsyncRetrying(
            retry=retry_condition,
            stop=stop_after_attempt(10),
            wait=wait_exponential(multiplier=0.4, min=0.4, max=60),
            reraise=True,
            before_sleep=before_sleep_log(logger, logging.WARNING),
        ):
            with attempt:
                await _download_once(url, file_location, session, effective_config)
    except KeyboardInterrupt:
        if file_location.is_file():
            logger.warning('Deleting partially downloaded file "%s"', file_location)
            silentremove(file_location)
        raise KeyboardInterrupt from None
    finally:
        if close_session:
            await session.close()


# ---------------------------------------------------------------------------
# Segmented download (disk-pipelined)
# ---------------------------------------------------------------------------

_SEGMENT_HTTP_CHUNK = 64 * 1024


async def _fetch_segment(
    session: aiohttp.ClientSession,
    url: str,
    headers: dict[str, str] | None = None,
) -> bytes:
    """Fetch a single segment into memory with retry.

    Args:
        session: aiohttp client session.
        url: Segment URL.
        headers: Optional HTTP headers.

    Returns:
        Raw segment bytes.
    """
    async for attempt in AsyncRetrying(
        retry=retry_if_exception_type((aiohttp.ClientError, TimeoutError)),
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=0.4, min=0.4, max=30),
        reraise=True,
        before_sleep=before_sleep_log(logger, logging.WARNING),
    ):
        with attempt:
            async with session.get(url, ssl=False, headers=headers) as response:
                response.raise_for_status()
                return await response.read()
    raise RuntimeError("Unreachable")  # pragma: no cover


async def _run_download_pipeline(
    urls: Sequence[str],
    output_paths: list[Path],
    session: aiohttp.ClientSession,
    max_concurrency: int,
    segment_processor: Callable[[bytes, int], bytes] | None,
    task_id: str | None,
    headers: dict[str, str] | None,
) -> None:
    """Fetch segments concurrently and write results in order.

    Args:
        urls: Ordered segment URLs.
        output_paths: Pre-computed output file paths (one per segment).
        session: aiohttp client session.
        max_concurrency: Parallel fetch cap.
        segment_processor: Optional per-segment transform.
        task_id: Progress task ID (may be ``None``).
        headers: Optional HTTP headers.
    """
    n = len(urls)
    buffers: list[bytes | None] = [None] * n
    ready = [anyio.Event() for _ in range(n)]
    limiter = anyio.CapacityLimiter(max(1, max_concurrency))

    async def _producer(i: int) -> None:
        async with limiter:
            buffers[i] = await _fetch_segment(session, urls[i], headers)
        ready[i].set()

    async def _consumer() -> None:
        for i in range(n):
            await ready[i].wait()
            data = buffers[i]
            assert data is not None
            buffers[i] = None
            if segment_processor is not None:
                data = segment_processor(data, i)
            await anyio.Path(output_paths[i]).write_bytes(data)
            del data
            if task_id:
                await advance(task_id, 1, n)

    async with anyio.create_task_group() as tg:
        for i in range(n):
            tg.start_soon(_producer, i)
        tg.start_soon(_consumer)


async def download_segments(
    urls: Sequence[str],
    target_dir: Path,
    *,
    session: aiohttp.ClientSession | None = None,
    max_concurrency: int = 4,
    segment_processor: Callable[[bytes, int], bytes] | None = None,
    task_id: str | None = None,
    headers: dict[str, str] | None = None,
) -> list[Path]:
    """Download segments in parallel and write to *target_dir*.

    Up to *max_concurrency* segments are fetched concurrently. A single
    sequential consumer applies the optional *segment_processor* and
    writes each result directly to *target_dir*. No intermediate temp
    files are created.

    Args:
        urls: Ordered sequence of segment URLs.
        target_dir: Directory to write segment files into.
        session: aiohttp client session. Created and closed internally
            when ``None``.
        max_concurrency: Maximum number of parallel segment downloads.
        segment_processor: Optional ``(data, segment_index) → bytes``
            transform applied in strict order. Stateful closures are
            safe (consumer processes 0, 1, 2, … sequentially).
        task_id: Task ID for progress reporting. Falls back to
            :func:`get_current_task` when ``None``.
        headers: Optional HTTP headers sent with each segment request.

    Returns:
        Ordered list of segment file paths.

    Raises:
        aiohttp.ClientError: After exhausting per-segment retries.
        KeyboardInterrupt: Partial output is removed before re-raising.
    """
    if not urls:
        return []

    effective_task_id = task_id or get_current_task()
    if effective_task_id:
        reset(effective_task_id)

    target_dir.mkdir(parents=True, exist_ok=True)

    close_session = False
    if session is None:
        session = create_aiohttp_session()
        close_session = True

    output_paths = [target_dir / f"seg_{i:05d}" for i in range(len(urls))]

    try:
        await _run_download_pipeline(
            urls,
            output_paths,
            session,
            max_concurrency,
            segment_processor,
            effective_task_id,
            headers,
        )
    except KeyboardInterrupt:
        silentremove(target_dir)
        raise KeyboardInterrupt from None
    finally:
        if close_session:
            await session.close()

    return output_paths


def compare_images(image_1: Path, image_2: Path) -> float:
    """Compares two images using root mean square difference.

    Args:
        image_1: Path to the first image.
        image_2: Path to the second image.

    Returns:
        The RMS difference between the two images.
    """
    with Image.open(str(image_1)) as im1, Image.open(str(image_2)) as im2:
        h = ImageChops.difference(im1, im2).convert("L").histogram()
        return math.sqrt(
            reduce(operator.add, map(lambda h, i: h * (i**2), h, range(256)))
            / (float(im1.size[0]) * im1.size[1])
        )


def get_image_resolution(image_location: Path) -> int:
    """Gets the width resolution of an image.

    Args:
        image_location: Path to the image file.

    Returns:
        The width of the image in pixels.
    """
    with Image.open(str(image_location)) as img:
        return img.size[0]


def silentremove(filename: Path) -> None:
    """Removes a file silently, ignoring errors if the file doesn't exist.

    Args:
        filename: Path to the file to remove.
    """
    try:
        filename.unlink()
    except OSError as e:
        if e.errno != errno.ENOENT:
            raise


def _get_module_session(
    temporary_settings: dict,
    module: str,
    global_mode: bool = False,
    session_name: str | None = None,
    create_if_missing: bool = False,
) -> dict | None:
    """Gets the session for a module from temporary settings.

    Args:
        temporary_settings: The full temporary settings dictionary.
        module: The module name.
        global_mode: Whether to use global mode.
        session_name: Specific session name to use (for multi-account support).
            If None, uses the "selected" session.
        create_if_missing: Whether to create the session if it doesn't exist.

    Returns:
        The session dictionary, or None if not found and not creating.
    """
    module_settings = temporary_settings["modules"].get(module, None)

    if module_settings:
        if global_mode:
            return module_settings
        else:
            target_session = session_name or module_settings["selected"]
            session = module_settings["sessions"].get(target_session)
            if session is None and (session_name and create_if_missing):
                module_settings["sessions"][session_name] = {}
                session = module_settings["sessions"][session_name]
            return session
    else:
        return None


def read_temporary_setting(
    settings_location: str,
    module: str,
    root_setting: str | None = None,
    setting: str | None = None,
    global_mode: bool = False,
    session_name: str | None = None,
) -> Any:
    """Reads a temporary setting from a JSON file.

    Args:
        settings_location: Path to the settings JSON file.
        module: The module name.
        root_setting: The root setting key.
        setting: The specific setting key within root_setting.
        global_mode: Whether to use global mode.
        session_name: Specific session name to use (for multi-account support).
            If None, uses the "selected" session.

    Returns:
        The setting value.

    Raises:
        TemporarySettingsError: If the module does not use temporary settings.
    """
    with open(settings_location, "rb") as f:
        temporary_settings = msgspec.json.decode(f.read())

    session = _get_module_session(
        temporary_settings, module, global_mode, session_name, create_if_missing=True
    )

    if session and root_setting:
        if setting:
            return (
                session[root_setting][setting]
                if root_setting in session and setting in session[root_setting]
                else None
            )
        else:
            return session.get(root_setting, None)
    elif root_setting and not session:
        raise TemporarySettingsError(module)
    else:
        return session


def set_temporary_setting(
    settings_location: str,
    module: str,
    root_setting: str,
    setting: str | None = None,
    value: Any = None,
    global_mode: bool = False,
    session_name: str | None = None,
) -> None:
    """Sets a temporary setting in a JSON file.

    Args:
        settings_location: Path to the settings JSON file.
        module: The module name.
        root_setting: The root setting key.
        setting: The specific setting key within root_setting.
        value: The value to set.
        global_mode: Whether to use global mode.
        session_name: Specific session name to use (for multi-account support).
            If None, uses the "selected" session.

    Raises:
        TemporarySettingsError: If the module does not use temporary settings.
    """
    with open(settings_location, "rb") as f:
        temporary_settings = msgspec.json.decode(f.read())

    session = _get_module_session(
        temporary_settings, module, global_mode, session_name, create_if_missing=True
    )

    if not session:
        raise TemporarySettingsError(module)
    if setting:
        if root_setting not in session:
            session[root_setting] = {}
        session[root_setting][setting] = value
    else:
        session[root_setting] = value
    with open(settings_location, "wb") as f:
        f.write(msgspec.json.encode(temporary_settings))


async def delete_path(path: Path) -> None:
    """Delete a file or directory asynchronously.

    Args:
        path: Path to delete (file or directory).
    """
    try:
        if path.is_dir():
            await asyncify(shutil.rmtree)(str(path))
            logger.debug("Deleted directory: %s", path)
        else:
            await anyio.Path(path).unlink(missing_ok=True)
            logger.debug("Deleted file: %s", path)
    except OSError:
        logger.exception("Failed to delete %s", path)


def _move_file_sync(src: Path, dst: Path) -> bool:
    """Synchronous file move operation.

    Args:
        src: Source path (file or directory).
        dst: Destination path.

    Returns:
        True if move was performed, False if skipped (same path).
    """
    src_resolved = src.resolve()
    dst_resolved = dst.resolve()

    # Skip if source and destination are the same
    if src_resolved == dst_resolved:
        return False

    # Ensure parent directory exists
    dst_resolved.parent.mkdir(parents=True, exist_ok=True)

    shutil.move(str(src), str(dst))
    return True


async def move_file(src: Path, dst: Path) -> None:
    """Move a file or directory asynchronously.

    This is an async wrapper around shutil.move that runs in a thread pool
    to avoid blocking the event loop. Handles both same-filesystem moves
    (which are instant) and cross-filesystem moves (which require copying).

    If source and destination are the same, this function does nothing.

    Args:
        src: Source path (file or directory).
        dst: Destination path.

    Raises:
        OSError: If the move operation fails.
    """
    moved = await asyncify(_move_file_sync)(src, dst)
    if moved:
        logger.debug("Moved %s to %s", src, dst)
    else:
        logger.debug("Source and destination are the same, skipping move: %s", src)


def compress_to_zip(
    source_paths: list[Path],
    archive_path: Path,
    compression_level: int = 0,
) -> None:
    """Compress files/directories into a ZIP archive.

    Args:
        source_paths: List of source directories/files to compress.
        archive_path: Output archive path.
        compression_level: Compression level (0-9, 0=store, 9=best). Defaults to 0.
    """
    compress_level = max(0, min(9, compression_level))
    compression = zipfile.ZIP_STORED if compress_level == 0 else zipfile.ZIP_DEFLATED

    with zipfile.ZipFile(
        archive_path,
        "w",
        compression=compression,
        compresslevel=compress_level if compress_level > 0 else None,
    ) as zf:
        for source_path in source_paths:
            if source_path.is_file():
                # Single file: add with just the filename
                zf.write(source_path, source_path.name)
            elif source_path.is_dir():
                # Directory: add all files recursively
                for file_path in source_path.rglob("*"):
                    if file_path.is_file():
                        # Preserve directory structure relative to source
                        arcname = Path(source_path.name) / file_path.relative_to(
                            source_path
                        )
                        zf.write(file_path, arcname)


def format_duration(seconds: int) -> str:
    """Formats seconds into a human-readable time string.

    Args:
        seconds: The number of seconds to format.

    Returns:
        A formatted time string (e.g., "1d:02h:30m:45s").
    """
    time_data = gmtime(seconds)
    time_format = "%Mm:%Ss"

    if time_data.tm_hour > 0:
        time_format = "%Hh:" + time_format

    if seconds >= 86400:
        days = seconds // 86400
        time_format = f"{days}d:" + time_format

    return strftime(time_format, time_data)
