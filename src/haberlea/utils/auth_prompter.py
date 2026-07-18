"""Interactive authentication prompting abstraction.

Some modules require an interactive login: TIDAL shows a device-link the
user must open, Amazon shows an OAuth URL and reads back the callback URL,
and KKBOX (in region-blocked fallback) prints a command and reads back a
JSON line. Historically these talked to the user directly through
``input()``/``print()``/``logger``, which only works on a TTY and breaks in
the WebUI.

``AuthPrompter`` decouples "ask the user something" from the transport. The
concrete implementation is injected at the composition root via constructor
dependency injection (see :class:`haberlea.utils.models.ModuleController`):

    * The CLI uses :class:`CliAuthPrompter` (stdout for display, stdin for
      input) — behaviour identical to the historical flows.
    * The WebUI injects its own prompter that pushes immutable prompt events
      into the download-service snapshot and awaits a UI response.

Modules never reference a transport; they only declare *what* they need from
the user.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from anyio.to_thread import run_sync

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from contextlib import AbstractAsyncContextManager

logger = logging.getLogger(__name__)


@runtime_checkable
class AuthPrompter(Protocol):
    """Channel for interactive prompts raised during a module login.

    Implementations are responsible for displaying messages/links to the
    user and collecting any input. All display side effects are confined to
    the implementation; callers stay transport-agnostic.
    """

    async def request_input(self, prompt: str, *, url: str = "") -> str:
        """Show a prompt (optionally with a link) and return user input.

        Args:
            prompt: Human-readable instructions shown to the user.
            url: Optional link the user must open before responding; shown
                as a clickable/copyable element in graphical front-ends.

        Returns:
            The text entered by the user, stripped of surrounding whitespace.
        """
        ...

    async def notify(self, message: str) -> None:
        """Display an informational status message to the user.

        Args:
            message: The message to surface.
        """
        ...

    def waiting(self, message: str, url: str) -> AbstractAsyncContextManager[None]:
        """Display a link plus a waiting indicator for the context's lifetime.

        Used by polling flows (e.g. TIDAL device authorization): the link and
        a "waiting" hint stay visible while the wrapped block runs and are
        cleared automatically on exit, whether it returns or raises.

        Args:
            message: Instructions shown alongside the link.
            url: The link the user must open to authorize.

        Returns:
            An async context manager wrapping the wait.
        """
        ...


def _blocking_input(prompt: str) -> str:
    """Read one stripped line from stdin (runs in a worker thread).

    Args:
        prompt: The prompt string printed before reading.

    Returns:
        The entered line, stripped of surrounding whitespace.
    """
    return input(prompt).strip()


class CliAuthPrompter:
    """Terminal-backed prompter: stdout for display, stdin for input.

    This is the default prompter and reproduces the historical CLI login
    behaviour for every module.
    """

    async def request_input(self, prompt: str, *, url: str = "") -> str:
        """Show the prompt (and any link) and read a line from stdin.

        Args:
            prompt: Instructions shown to the user.
            url: Optional link displayed above the input cursor.

        Returns:
            The entered line, stripped of surrounding whitespace.
        """
        full_prompt = f"{prompt}\n\n    {url}\n\n> " if url else f"{prompt}\n> "
        return await run_sync(_blocking_input, full_prompt)

    async def notify(self, message: str) -> None:
        """Log an informational status message.

        Args:
            message: The message to log.
        """
        logger.info("%s", message)

    @asynccontextmanager
    async def waiting(self, message: str, url: str) -> AsyncGenerator[None]:
        """Log the link and a waiting hint for the duration of the context.

        Args:
            message: Instructions shown alongside the link.
            url: The link the user must open to authorize.

        Yields:
            None — control returns to the wrapped polling block.
        """
        logger.info("%s\n\n    %s\n", message, url)
        logger.info("Waiting for authorization...")
        yield
