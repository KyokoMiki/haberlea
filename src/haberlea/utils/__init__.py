"""Utility modules for Haberlea.

This package provides utility functions, models, and exception classes
used throughout the Haberlea application.
"""

from .exceptions import (
    AlbumDownloadError,
    APIError,
    AuthenticationError,
    ConfigurationError,
    DownloadError,
    DownloadErrorGroup,
    FileOperationError,
    HaberleaError,
    InvalidHashTypeError,
    InvalidInput,
    InvalidModuleError,
    InvalidTrackError,
    InvalidURLError,
    ModuleAPIError,
    ModuleAuthError,
    ModuleDoesNotSupportAbility,
    ModuleError,
    ModuleSettingsNotSet,
    RateLimitError,
    RegionRestrictedError,
    SessionExpiredError,
    TagSavingFailure,
    TemporarySettingsError,
    TrackDownloadError,
    ValidationError,
    raise_with_context,
)
from .tempfile_manager import TempFileManager

__all__ = [
    # Exceptions
    "HaberleaError",
    "ModuleError",
    "AuthenticationError",
    "ModuleAuthError",
    "SessionExpiredError",
    "APIError",
    "ModuleAPIError",
    "RegionRestrictedError",
    "RateLimitError",
    "ConfigurationError",
    "InvalidModuleError",
    "ModuleSettingsNotSet",
    "ModuleDoesNotSupportAbility",
    "TemporarySettingsError",
    "DownloadError",
    "TrackDownloadError",
    "AlbumDownloadError",
    "InvalidTrackError",
    "ValidationError",
    "InvalidInput",
    "InvalidURLError",
    "InvalidHashTypeError",
    "FileOperationError",
    "TagSavingFailure",
    "DownloadErrorGroup",
    "raise_with_context",
    # Temp file management
    "TempFileManager",
]
