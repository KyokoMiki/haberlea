"""Authentication module for Haberlea WebUI."""

from secrets import compare_digest

from fastapi import Request
from fastapi.responses import RedirectResponse
from nicegui import app, ui
from starlette.middleware.base import BaseHTTPMiddleware

from haberlea.i18n import _
from haberlea.utils.settings import settings

# Routes that don't require authentication
UNRESTRICTED_ROUTES = {"/login"}


class AuthMiddleware(BaseHTTPMiddleware):
    """Middleware to restrict access to authenticated users only."""

    async def dispatch(self, request: Request, call_next):
        """Dispatches the request with authentication check."""
        # Skip auth check if authentication is disabled
        if not settings.global_settings.webui.auth_enabled:
            return await call_next(request)

        # Allow unrestricted routes and NiceGUI internals
        path = request.url.path
        if path.startswith("/_nicegui") or path in UNRESTRICTED_ROUTES:
            return await call_next(request)

        # Check authentication status
        if not app.storage.user.get("authenticated", False):
            return RedirectResponse(f"/login?redirect_to={path}")

        return await call_next(request)


def create_login_page():
    """Creates the login page route."""

    @ui.page("/login")
    def login_page(redirect_to: str = "/") -> RedirectResponse | None:
        """Login page."""
        webui_settings = settings.global_settings.webui

        def try_login() -> None:
            username = username_input.value or ""
            password = password_input.value or ""
            if compare_digest(username, webui_settings.username) and compare_digest(
                password, webui_settings.password
            ):
                app.storage.user.update({"username": username, "authenticated": True})
                # Validate redirect_to to prevent open redirect
                safe_redirect = redirect_to
                if not redirect_to.startswith("/") or redirect_to.startswith("//"):
                    safe_redirect = "/"
                # Also check for scheme (http://, https://, etc.)
                if "://" in redirect_to:
                    safe_redirect = "/"
                ui.navigate.to(safe_redirect)
            else:
                ui.notify(_("Invalid username or password"), color="negative")

        # Already authenticated, redirect to main page
        if app.storage.user.get("authenticated", False):
            return RedirectResponse("/")

        # Login form (centered card)
        with ui.card().classes("absolute-center w-80 p-6 shadow-lg"):
            ui.label(_("Haberlea Login")).classes(
                "text-xl font-bold mb-6 text-center w-full"
            )

            username_input = (
                ui.input(_("Username"))
                .props("autofocus")
                .on("keydown.enter", try_login)
                .classes("w-full mb-2")
            )
            password_input = (
                ui.input(_("Password"), password=True, password_toggle_button=True)
                .on("keydown.enter", try_login)
                .classes("w-full mb-6")
            )

            ui.button(_("Login"), on_click=try_login).classes("w-full")

        return None
