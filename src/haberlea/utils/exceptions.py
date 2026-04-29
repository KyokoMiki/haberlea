"""Custom exception hierarchy for Haberlea.

This module defines a structured exception hierarchy following Python 3.14
best practices, including:
- Clear exception hierarchy with base classes
- Rich context information via attributes
- Exception chaining support
- Type hints and Google-style docstrings
"""

import inspect
from pathlib import Path
from typing import Any


def get_module_name() -> str:
    """Gets the module name from the call stack.

    Inspects the call stack to find the module name from interface.py files.

    Returns:
        The module name extracted from the file path, or "Unknown" if not found.
    """
    result = next(
        (s.filename for s in inspect.stack() if "interface.py" in s.filename), None
    )
    if result is None:
        return "Unknown"
    return Path(result).parent.name


# =============================================================================
# Base Exception Classes
# =============================================================================


class HaberleaError(Exception):
    """Base exception for all Haberlea errors.

    All custom exceptions in Haberlea should inherit from this class
    to enable unified exception handling.

    Attributes:
        message: Human-readable error description.
    """

    def __init__(self, message: str = "An error occurred in Haberlea") -> None:
        """Initializes the base exception.

        Args:
            message: Human-readable error description.
        """
        self.message = message
        super().__init__(message)


class ModuleError(HaberleaError):
    """Base exception for module-related errors.

    Attributes:
        module_name: Name of the module where the error occurred.
        message: Human-readable error description.
    """

    def __init__(
        self,
        message: str = "A module error occurred",
        module_name: str | None = None,
    ) -> None:
        """Initializes the module error.

        Args:
            message: Human-readable error description.
            module_name: Name of the module. Auto-detected if not provided.
        """
        self.module_name = module_name or get_module_name()
        full_message = f"[{self.module_name}] {message}"
        super().__init__(full_message)


# =============================================================================
# Authentication Errors
# =============================================================================


class AuthenticationError(ModuleError):
    """Base exception for authentication-related errors."""

    pass


class ModuleAuthError(AuthenticationError):
    """Exception raised when module authentication fails.

    Attributes:
        module_name: Name of the module with invalid credentials.
    """

    def __init__(self, module_name: str | None = None) -> None:
        """Initializes the authentication error.

        Args:
            module_name: Name of the module. Auto-detected if not provided.
        """
        super().__init__(
            message="Invalid login details",
            module_name=module_name,
        )


class SessionExpiredError(AuthenticationError):
    """Exception raised when a session has expired.

    Attributes:
        module_name: Name of the module with expired session.
    """

    def __init__(self, module_name: str | None = None) -> None:
        """Initializes the session expired error.

        Args:
            module_name: Name of the module. Auto-detected if not provided.
        """
        super().__init__(
            message="Session has expired, please re-authenticate",
            module_name=module_name,
        )


# =============================================================================
# API Errors
# =============================================================================


class APIError(ModuleError):
    """Base exception for API-related errors."""

    pass


class ModuleAPIError(APIError):
    """Exception raised when a module API call fails.

    Attributes:
        error_code: HTTP or API error code.
        error_message: Error message from the API.
        api_endpoint: The API endpoint that failed.
        module_name: Name of the module.
    """

    def __init__(
        self,
        error_code: int,
        error_message: str,
        api_endpoint: str,
        module_name: str | None = None,
    ) -> None:
        """Initializes the API error.

        Args:
            error_code: HTTP or API error code.
            error_message: Error message from the API.
            api_endpoint: The API endpoint that failed.
            module_name: Name of the module. Auto-detected if not provided.
        """
        self.error_code = error_code
        self.error_message = error_message
        self.api_endpoint = api_endpoint
        super().__init__(
            message=f"Error {error_code}: {error_message} (endpoint: {api_endpoint})",
            module_name=module_name,
        )


class RegionRestrictedError(APIError):
    """Exception raised when content is region-restricted.

    This error indicates that the current account cannot access the content
    due to geographic restrictions. The system should try other accounts.

    Attributes:
        content_id: ID of the restricted content.
        content_type: Type of content (album, track, etc.).
        module_name: Name of the module.
    """

    def __init__(
        self,
        content_id: str,
        content_type: str,
        module_name: str | None = None,
    ) -> None:
        """Initializes the region restricted error.

        Args:
            content_id: ID of the restricted content.
            content_type: Type of content (album, track, etc.).
            module_name: Name of the module. Auto-detected if not provided.
        """
        self.content_id = content_id
        self.content_type = content_type
        super().__init__(
            message=f"{content_type} '{content_id}' is not available in this region",
            module_name=module_name,
        )


class RateLimitError(APIError):
    """Exception raised when API rate limit is exceeded.

    Attributes:
        retry_after: Seconds to wait before retrying, if provided by API.
        module_name: Name of the module.
    """

    def __init__(
        self,
        retry_after: int | None = None,
        module_name: str | None = None,
    ) -> None:
        """Initializes the rate limit error.

        Args:
            retry_after: Seconds to wait before retrying.
            module_name: Name of the module. Auto-detected if not provided.
        """
        self.retry_after = retry_after
        msg = "Rate limit exceeded"
        if retry_after:
            msg += f", retry after {retry_after} seconds"
        super().__init__(message=msg, module_name=module_name)


# =============================================================================
# Module Configuration Errors
# =============================================================================


class ConfigurationError(ModuleError):
    """Base exception for configuration-related errors."""

    pass


class InvalidModuleError(ConfigurationError):
    """Exception raised when a module does not exist or cannot be loaded.

    Attributes:
        module_name: Name of the invalid module.
    """

    def __init__(self, module_name: str) -> None:
        """Initializes the invalid module error.

        Args:
            module_name: Name of the module that doesn't exist.
        """
        super().__init__(
            message=f'Module "{module_name}" does not exist or cannot be loaded',
            module_name=module_name,
        )


class ModuleSettingsNotSet(ConfigurationError):
    """Exception raised when required module settings are not configured.

    Attributes:
        setting_name: Name of the missing setting.
        module_name: Name of the module.
    """

    def __init__(
        self,
        module_name: str,
        setting_name: str,
    ) -> None:
        """Initializes the settings not set error.

        Args:
            module_name: Name of the module.
            setting_name: Name of the missing setting.
        """
        self.setting_name = setting_name

        message = (
            f'Setting "{setting_name}" is not set. '
            f"Please add it to the module settings in config/settings.toml."
        )
        super().__init__(message=message, module_name=module_name)


class ModuleDoesNotSupportAbility(ConfigurationError):
    """Exception raised when a module does not support a requested ability.

    Attributes:
        ability: The unsupported ability/feature.
        module_name: Name of the module.
    """

    def __init__(self, module_name: str, ability: str) -> None:
        """Initializes the unsupported ability error.

        Args:
            module_name: Name of the module.
            ability: The ability that is not supported.
        """
        self.ability = ability
        super().__init__(
            message=f'Does not support "{ability}"',
            module_name=module_name,
        )


class TemporarySettingsError(ConfigurationError):
    """Exception raised when temporary settings operations fail.

    Attributes:
        module_name: Name of the module.
    """

    def __init__(self, module_name: str | None = None) -> None:
        """Initializes the temporary settings error.

        Args:
            module_name: Name of the module. Auto-detected if not provided.
        """
        super().__init__(
            message="Module does not use temporary settings",
            module_name=module_name,
        )


# =============================================================================
# Download Errors
# =============================================================================


class DownloadError(HaberleaError):
    """Base exception for download-related errors."""

    pass


class TrackDownloadError(DownloadError):
    """Exception raised when a track download fails.

    Attributes:
        track_id: ID of the track that failed to download.
        reason: Reason for the failure.
    """

    def __init__(self, track_id: str, reason: str = "Unknown error") -> None:
        """Initializes the track download error.

        Args:
            track_id: ID of the track that failed.
            reason: Reason for the failure.
        """
        self.track_id = track_id
        self.reason = reason
        super().__init__(f"Failed to download track {track_id}: {reason}")


class AlbumDownloadError(DownloadError):
    """Exception raised when an album download fails.

    Attributes:
        album_id: ID of the album that failed to download.
        failed_tracks: List of track IDs that failed.
    """

    def __init__(
        self,
        album_id: str,
        failed_tracks: list[str] | None = None,
        reason: str = "Unknown error",
    ) -> None:
        """Initializes the album download error.

        Args:
            album_id: ID of the album that failed.
            failed_tracks: List of track IDs that failed.
            reason: Reason for the failure.
        """
        self.album_id = album_id
        self.failed_tracks = failed_tracks or []
        self.reason = reason
        msg = f"Failed to download album {album_id}: {reason}"
        if self.failed_tracks:
            msg += f" (failed tracks: {len(self.failed_tracks)})"
        super().__init__(msg)


class InvalidTrackError(DownloadError):
    """Exception raised when a track is invalid or unavailable.

    Attributes:
        track_id: ID of the invalid track.
        reason: Reason why the track is invalid.
    """

    def __init__(
        self, track_id: str, reason: str = "Track is invalid or unavailable"
    ) -> None:
        """Initializes the invalid track error.

        Args:
            track_id: ID of the invalid track.
            reason: Reason why the track is invalid.
        """
        self.track_id = track_id
        self.reason = reason
        super().__init__(f"Invalid track {track_id}: {reason}")


# =============================================================================
# Input/Validation Errors
# =============================================================================


class ValidationError(HaberleaError):
    """Base exception for validation-related errors."""

    pass


class InvalidInput(ValidationError):
    """Exception raised when user provides invalid input.

    Attributes:
        field: The field with invalid input, if applicable.
        value: The invalid value, if applicable.
    """

    def __init__(
        self,
        message: str = "Invalid input provided",
        field: str | None = None,
        value: Any = None,
    ) -> None:
        """Initializes the invalid input error.

        Args:
            message: Human-readable error description.
            field: The field with invalid input.
            value: The invalid value.
        """
        self.field = field
        self.value = value
        if field:
            message = f"Invalid input for '{field}': {message}"
        super().__init__(message)


class InvalidURLError(ValidationError):
    """Exception raised when a URL cannot be parsed or is invalid.

    Attributes:
        url: The invalid URL.
        reason: Reason why the URL is invalid.
    """

    def __init__(self, url: str, reason: str = "Cannot parse URL") -> None:
        """Initializes the invalid URL error.

        Args:
            url: The invalid URL.
            reason: Reason why the URL is invalid.
        """
        self.url = url
        self.reason = reason
        super().__init__(f"Invalid URL '{url}': {reason}")


class InvalidHashTypeError(ValidationError):
    """Exception raised when an invalid hash type is specified.

    Attributes:
        hash_type: The invalid hash type.
        supported_types: List of supported hash types.
    """

    def __init__(
        self,
        hash_type: str,
        supported_types: list[str] | None = None,
    ) -> None:
        """Initializes the invalid hash type error.

        Args:
            hash_type: The invalid hash type.
            supported_types: List of supported hash types.
        """
        self.hash_type = hash_type
        self.supported_types = supported_types or ["MD5"]
        msg = f"Invalid hash type '{hash_type}'"
        if self.supported_types:
            msg += f", supported types: {', '.join(self.supported_types)}"
        super().__init__(msg)


# =============================================================================
# File/Tagging Errors
# =============================================================================


class FileOperationError(HaberleaError):
    """Base exception for file operation errors."""

    pass


class TagSavingFailure(FileOperationError):
    """Exception raised when saving tags to a file fails.

    Attributes:
        file_path: Path to the file that failed.
        reason: Reason for the failure.
    """

    def __init__(
        self,
        file_path: str | None = None,
        reason: str = "Failed to save tags",
    ) -> None:
        """Initializes the tag saving failure error.

        Args:
            file_path: Path to the file that failed.
            reason: Reason for the failure.
        """
        self.file_path = file_path
        self.reason = reason
        msg = reason
        if file_path:
            msg = f"Failed to save tags to '{file_path}': {reason}"
        super().__init__(msg)


# =============================================================================
# Exception Groups Support (Python 3.11+)
# =============================================================================


class DownloadErrorGroup(ExceptionGroup):
    """Exception group for multiple download failures.

    Use this when multiple tracks fail during a batch download operation.
    Supports except* syntax for selective handling.

    Example:
        >>> try:
        ...     await download_album(album_id)
        ... except* TrackDownloadError as eg:
        ...     for exc in eg.exceptions:
        ...         log_failed_track(exc.track_id)
        ... except* APIError as eg:
        ...     for exc in eg.exceptions:
        ...         log_api_error(exc)
    """

    def __new__(
        cls,
        message: str,
        exceptions: list[DownloadError],
    ) -> "DownloadErrorGroup":
        """Creates a new DownloadErrorGroup.

        Args:
            message: Description of the error group.
            exceptions: List of download errors.

        Returns:
            A new DownloadErrorGroup instance.
        """
        return super().__new__(cls, message, exceptions)


# =============================================================================
# Utility Functions
# =============================================================================


def raise_with_context(
    new_exception: Exception,
    original_exception: Exception | None = None,
) -> None:
    """Raises an exception with proper chaining.

    This utility function ensures proper exception chaining using 'raise from'.

    Args:
        new_exception: The new exception to raise.
        original_exception: The original exception to chain from.

    Raises:
        The new_exception with original_exception as its cause.

    Example:
        >>> try:
        ...     api_call()
        ... except requests.RequestException as e:
        ...     raise_with_context(
        ...         ModuleAPIError(500, "API failed", "/endpoint"),
        ...         e
        ...     )
    """
    raise new_exception from original_exception
