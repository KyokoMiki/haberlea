"""Session management for module authentication and JWT tokens."""

import base64
import inspect
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import msgspec

from haberlea.downloader.contexts import LoginContext
from haberlea.utils.models import (
    ManualEnum,
    ModuleFlags,
    ModuleInformation,
    TemporarySettingsController,
)
from haberlea.utils.settings import SESSION_PATH
from haberlea.utils.utils import hash_string

logger = logging.getLogger(__name__)

_timestamp_correction: int = 0


def _get_utc_timestamp() -> int:
    """Gets the current UTC timestamp with correction applied."""
    return int(datetime.now(UTC).timestamp()) + _timestamp_correction


def get_utc_timestamp() -> int:
    """Public accessor for UTC timestamp with correction."""
    return _get_utc_timestamp()


class SessionManager:
    """Manages module authentication sessions and JWT tokens.

    Single responsibility: session lifecycle (create, validate, refresh, clear).
    """

    def __init__(self, session_path: Path = SESSION_PATH) -> None:
        self.session_path = session_path

    # --- Pure functions (no I/O) ---

    def is_jwt_expired(self, bearer: str) -> bool:
        """Checks if a JWT bearer token is expired.

        Args:
            bearer: JWT bearer token string.

        Returns:
            True if the token is expired or invalid.
        """
        try:
            parts = bearer.split(".")
            if len(parts) < 2:
                return True

            payload = parts[1]
            padding = len(payload) % 4
            if padding:
                payload += "=" * (4 - padding)

            decoded = msgspec.json.decode(base64.b64decode(payload))
            time_left = decoded["exp"] - _get_utc_timestamp()
            return time_left <= 0
        except Exception:
            return True

    def should_refresh_jwt(
        self, module_info: ModuleInformation, session: dict[str, Any] | None
    ) -> bool:
        """Checks if JWT token should be refreshed.

        Args:
            module_info: Module information.
            session: Session data dictionary.

        Returns:
            True if JWT should be refreshed.
        """
        if not session:
            return False

        has_jwt = bool(module_info.flags & ModuleFlags.enable_jwt_system)
        return bool(has_jwt and session.get("refresh") and not session.get("bearer"))

    def should_clear_session(
        self,
        session_data: dict[str, Any],
        module_info: ModuleInformation,
        module_settings: dict[str, Any],
    ) -> bool:
        """Determines if a session should be cleared.

        Args:
            session_data: Current session data.
            module_info: Module information.
            module_settings: Account settings.

        Returns:
            True if session should be cleared.
        """
        if module_info.login_behaviour is not ManualEnum.haberlea:
            return False

        hashes = {k: hash_string(str(v)) for k, v in module_settings.items()}
        existing_hashes = session_data.get("hashes")

        if not existing_hashes:
            return True

        return any(
            k not in hashes or hashes[k] != v
            for k, v in existing_hashes.items()
            if k in module_info.session_settings
        )

    # --- I/O boundary ---

    def load_sessions(self) -> dict[str, Any]:
        """Loads all sessions from disk.

        Returns:
            Sessions dictionary, empty dict if file doesn't exist.
        """
        if not self.session_path.exists():
            return {}
        return msgspec.json.decode(self.session_path.read_bytes())

    def save_sessions(self, sessions: dict[str, Any]) -> None:
        """Persists sessions to disk.

        Args:
            sessions: Sessions dictionary to save.
        """
        self.session_path.write_bytes(msgspec.json.encode(sessions))

    async def handle_module_auth(
        self,
        ctx: LoginContext,
    ) -> None:
        """Handles module authentication if required.

        Args:
            ctx: Login context bundling module, instance, info, and account.
        """
        if ctx.module_info.login_behaviour is not ManualEnum.haberlea:
            return

        controller = TemporarySettingsController(
            ctx.module_name, str(self.session_path), ctx.account_index
        )
        session = controller.read_session()

        if session and session.get("clear_session"):
            await self._perform_login(ctx, session)

        if self.should_refresh_jwt(ctx.module_info, session):
            refresh_fn = getattr(ctx.loaded_module, "refresh_login", None)
            if refresh_fn is not None:
                if inspect.iscoroutinefunction(refresh_fn):
                    await refresh_fn()
                else:
                    result = refresh_fn()
                    if inspect.iscoroutine(result):
                        await result

    async def _perform_login(
        self,
        ctx: LoginContext,
        session: dict[str, Any],
    ) -> None:
        """Performs login for a module.

        Args:
            ctx: Login context bundling module, instance, info, and account.
            session: Session data dictionary.
        """
        hashes = {k: hash_string(str(v)) for k, v in ctx.account_settings.items()}
        controller = TemporarySettingsController(
            ctx.module_name, str(self.session_path), ctx.account_index
        )

        if session.get("hashes"):
            needs_login = any(
                k not in hashes or hashes[k] != v
                for k, v in session["hashes"].items()
                if k in ctx.module_info.session_settings
            )
        else:
            needs_login = True

        if not needs_login:
            return

        username: str = (
            ctx.account_settings.get("email")
            or ctx.account_settings.get("username")
            or ""
        )
        password: str = ctx.account_settings.get("password", "")
        account_name: str = ctx.account_settings.get("name", "") or ""

        logger.info(
            "Logging into %s (account %d, name=%r, username=%r)",
            ctx.module_info.service_name,
            ctx.account_index,
            account_name,
            username,
        )

        try:
            await ctx.loaded_module.login(username, password)
        except Exception:
            controller.set_raw("hashes", {})
            logger.error(
                "Login failed for %s account %d (name=%r, username=%r)",
                ctx.module_info.service_name,
                ctx.account_index,
                account_name,
                username,
            )
            raise

        controller.set_raw("hashes", hashes)

    def clear_module_session(self, module: str) -> bool:
        """Clears all session data for a specific module.

        Args:
            module: Module name to clear session for.

        Returns:
            True if session was cleared successfully, False if module not found.
        """
        sessions = self.load_sessions()

        if "modules" not in sessions or module not in sessions["modules"]:
            return False

        sessions["modules"][module] = {}
        self.save_sessions(sessions)
        return True

    def update_sessions(
        self,
        module_list: set[str],
        module_settings: dict[str, ModuleInformation],
        module_accounts: dict[str, list[dict[str, Any]]],
    ) -> dict[str, Any]:
        """Updates session storage for all modules.

        Args:
            module_list: Set of registered module names.
            module_settings: Module information dictionary.
            module_accounts: Module settings with list of accounts per module.

        Returns:
            Updated sessions dictionary.
        """
        sessions = self.load_sessions()

        if "modules" not in sessions:
            sessions = {"modules": {}}
        sessions.pop("advancedmode", None)

        for module_name in module_list:
            accounts = module_accounts.get(module_name, [])

            sessions["modules"][module_name] = self._update_module_session(
                module_name,
                module_settings[module_name],
                sessions["modules"].get(module_name),
                accounts,
            )

        return sessions

    def _update_module_session(
        self,
        module_name: str,
        module_info: ModuleInformation,
        existing_session: dict[str, Any] | None,
        accounts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Updates a single module's session data.

        Args:
            module_name: Name of the module.
            module_info: Module information.
            existing_session: Existing session data if any.
            accounts: List of account settings for this module.

        Returns:
            Updated session dictionary for the module.
        """
        session = existing_session or {
            "selected": "default",
            "sessions": {},
        }

        self._update_global_storage(session, module_info)

        if "sessions" not in session:
            session["sessions"] = {}

        for idx, account_settings in enumerate(accounts):
            session_name = "default" if idx == 0 else f"account_{idx}"

            if session_name not in session["sessions"]:
                session["sessions"][session_name] = {}

            self._update_session_entry(
                session["sessions"][session_name],
                module_info,
                account_settings,
            )

        if not accounts and "default" not in session["sessions"]:
            session["sessions"]["default"] = {}

        return session

    def _update_global_storage(
        self, session: dict[str, Any], module_info: ModuleInformation
    ) -> None:
        """Updates global storage variables in session.

        Args:
            session: Session data dictionary.
            module_info: Module information.
        """
        storage_vars = module_info.global_storage_variables
        if not storage_vars:
            return

        existing_data = session.get("custom_data", {})
        session["custom_data"] = {
            k: v for k, v in existing_data.items() if k in storage_vars
        }

    def _update_session_entry(
        self,
        session_data: dict[str, Any],
        module_info: ModuleInformation,
        module_settings: dict[str, Any],
    ) -> None:
        """Updates a single session entry.

        Args:
            session_data: Session data to update.
            module_info: Module information.
            module_settings: Account settings.
        """
        clear_session = self.should_clear_session(
            session_data, module_info, module_settings
        )
        session_data["clear_session"] = clear_session

        self._update_jwt_tokens(session_data, module_info, clear_session)
        self._update_session_storage(session_data, module_info, clear_session)

    def _update_jwt_tokens(
        self,
        session_data: dict[str, Any],
        module_info: ModuleInformation,
        clear_session: bool,
    ) -> None:
        """Updates JWT tokens in session data.

        Args:
            session_data: Session data to update.
            module_info: Module information.
            clear_session: Whether session is being cleared.
        """
        has_jwt = bool(module_info.flags & ModuleFlags.enable_jwt_system)

        if not has_jwt:
            session_data.pop("bearer", None)
            session_data.pop("refresh", None)
            return

        bearer = session_data.get("bearer")
        if bearer and not clear_session:
            if self.is_jwt_expired(bearer):
                session_data["bearer"] = ""
        else:
            session_data["bearer"] = ""
            session_data["refresh"] = ""

    def _update_session_storage(
        self,
        session_data: dict[str, Any],
        module_info: ModuleInformation,
        clear_session: bool,
    ) -> None:
        """Updates session storage variables.

        Args:
            session_data: Session data to update.
            module_info: Module information.
            clear_session: Whether session is being cleared.
        """
        storage_vars = module_info.session_storage_variables

        if not storage_vars:
            session_data.pop("custom_data", None)
            return

        if clear_session:
            session_data["custom_data"] = {}
            return

        existing_data = session_data.get("custom_data", {})
        session_data["custom_data"] = {
            k: v for k, v in existing_data.items() if k in storage_vars
        }
