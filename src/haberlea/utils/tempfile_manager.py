"""Modern temporary file management for Haberlea.

This module provides a centralized, type-safe, and context-managed approach
to temporary file handling using anyio for native async support.

Features:
- Native async temporary file/directory operations via anyio
- Automatic cleanup via anyio's built-in context managers
- pathlib.Path integration
- Configurable base directory from settings
"""

import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import gettempdir

from anyio import NamedTemporaryFile, TemporaryDirectory


class TempFileManager:
    """Centralized temporary file manager using anyio's automatic cleanup.

    This class provides both automatic cleanup (via anyio context managers) and
    manual path generation depending on the use case.

    Use anyio context managers when:
    - File lifetime is scoped to a specific operation
    - Automatic cleanup is desired

    Use path generation when:
    - File needs to persist across multiple operations
    - File lifetime is managed by caller (e.g., caching, moving to final location)

    Example:
        ```python
        tmp = TempFileManager()

        # Auto-cleanup: file deleted when context exits
        async with tmp.file(suffix=".flac") as path:
            await download_to_file(url, path)
            await process_file(path)

        # Manual cleanup: caller manages lifetime
        temp_path = tmp.get_temp_filename(suffix=".flac")
        await download_file(url, str(temp_path))
        # ... use temp_path ...
        temp_path.unlink()  # Manual cleanup
        ```
    """

    def __init__(
        self,
        base_dir: Path | str | None = None,
        prefix: str = "haberlea_",
    ) -> None:
        """Initialize the temporary file manager.

        Args:
            base_dir: Base directory for temporary files. If None, uses the
                temp_path from settings, or system temp directory if not configured.
            prefix: Prefix for temporary file/directory names.
        """
        if base_dir:
            self._base_dir = Path(base_dir)
        else:
            self._base_dir = Path(gettempdir())

        # Ensure base directory exists
        self._base_dir.mkdir(parents=True, exist_ok=True)

        self._prefix = prefix

    @asynccontextmanager
    async def file(
        self,
        suffix: str = "",
        prefix: str | None = None,
    ) -> AsyncGenerator[Path]:
        """Create a temporary file path with automatic cleanup.

        The file is created but immediately closed, allowing the caller to
        open it as needed. The file will be automatically deleted when
        exiting the context.

        Args:
            suffix: File suffix (e.g., ".flac", ".jpg").
            prefix: File prefix. Defaults to manager's prefix.

        Yields:
            Path to the temporary file.
        """
        file_prefix = prefix or self._prefix
        async with NamedTemporaryFile(
            mode="wb",
            suffix=suffix,
            prefix=file_prefix,
            dir=str(self._base_dir),
            delete=True,
            delete_on_close=False,
        ) as f:
            await f.aclose()
            yield Path(str(f.name))

    @asynccontextmanager
    async def dir(
        self,
        suffix: str = "",
        prefix: str | None = None,
    ) -> AsyncGenerator[Path]:
        """Create a temporary directory with automatic cleanup via anyio.

        Args:
            suffix: Directory suffix.
            prefix: Directory prefix. Defaults to manager's prefix.

        Yields:
            Path to the temporary directory.
        """
        dir_prefix = prefix or self._prefix
        async with TemporaryDirectory(
            suffix=suffix,
            prefix=dir_prefix,
            dir=str(self._base_dir),
        ) as temp_dir:
            yield Path(str(temp_dir))

    def get_temp_filename(self, suffix: str = "", prefix: str | None = None) -> Path:
        """Generate a temporary file path without creating or tracking the file.

        The caller is responsible for creating and cleaning up the file.
        Use this when file lifetime needs to be managed manually, such as:
        - Files that will be moved to a final location
        - Files that need to be cached across operations
        - Files passed to external code that creates them

        Args:
            suffix: File suffix (e.g., ".flac", ".jpg").
            prefix: File prefix. Defaults to manager's prefix.

        Returns:
            Path to a non-existent temporary file location.
        """
        file_prefix = prefix or self._prefix
        filename = f"{file_prefix}{uuid.uuid4()}{suffix}"
        return self._base_dir / filename

    def get_temp_dirname(self, suffix: str = "", prefix: str | None = None) -> Path:
        """Generate a temporary directory path without creating or tracking it.

        The caller is responsible for creating and cleaning up the directory.

        Args:
            suffix: Directory suffix.
            prefix: Directory prefix. Defaults to manager's prefix.

        Returns:
            Path to a non-existent temporary directory location.
        """
        dir_prefix = prefix or self._prefix
        dirname = f"{dir_prefix}{uuid.uuid4()}{suffix}"
        return self._base_dir / dirname
