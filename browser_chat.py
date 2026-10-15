"""Dedicated visible browser lifecycle; site readiness belongs to Task 2."""

from __future__ import annotations

import asyncio
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Playwright

START_TIMEOUT_SECONDS = 30
CLOSE_TIMEOUT_SECONDS = 20


class BrowserStatus(str, Enum):
    STARTING = "starting"
    READY = "ready"
    PAUSED = "paused"
    STOPPING = "stopping"


class ChatGPTBrowser:
    """Own one dedicated persistent context without claiming site readiness.

    Args:
        data_dir: Plugin data directory from StarTools.get_data_dir() in the
            future host integration; never a daily browser profile directory.
        channel: Installed browser channel. Only Chrome is currently tested.
    """

    def __init__(self, data_dir: Path, channel: str = "chrome") -> None:
        self.profile_dir = Path(data_dir).resolve() / "browser_profile"
        self.channel = channel
        self.status = BrowserStatus.PAUSED
        self.pause_reason = "config"
        self.context: BrowserContext | None = None
        self._driver: Playwright | None = None
        self._startup_task: asyncio.Task[Playwright | None] | None = None
        self._abandon_start = False
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """Open the owned browser, keeping admission paused until site checks.

        Raises:
            RuntimeError: Playwright is missing; installation is always manual.
            Exception: Browser startup failed after releasing owned resources.
        """
        async with self._lock:
            if self.context is not None:
                return
            if self._startup_task is not None:
                await self._release()
            self.status = BrowserStatus.STARTING
            self.pause_reason = "browser"
            try:
                try:
                    from playwright.async_api import async_playwright
                except ImportError:
                    raise RuntimeError(
                        "Playwright is missing. Install this plugin's requirements.txt "
                        "with the host Python interpreter."
                    ) from None
                self.profile_dir.mkdir(parents=True, exist_ok=True)
                self._abandon_start = False
                self._startup_task = asyncio.create_task(
                    self._connect_driver(async_playwright())
                )
                self._driver = await asyncio.wait_for(
                    asyncio.shield(self._startup_task), START_TIMEOUT_SECONDS
                )
                self.context = await asyncio.wait_for(
                    self._driver.chromium.launch_persistent_context(
                        user_data_dir=str(self.profile_dir),
                        channel=self.channel,
                        headless=False,
                        timeout=START_TIMEOUT_SECONDS * 1000,
                    ),
                    START_TIMEOUT_SECONDS,
                )
            except BaseException:
                # A cancelled launch must also stop the owned driver subprocess.
                await self._release()
                raise
            self.status = BrowserStatus.PAUSED
            self.pause_reason = "config"

    async def close(self) -> None:
        """Release owned resources; repeated calls are harmless.

        Raises:
            Exception: Context or driver shutdown failed or exceeded its timeout.
        """
        async with self._lock:
            self.status = BrowserStatus.STOPPING
            await self._release()

    async def _connect_driver(self, manager) -> Playwright | None:
        """Finish the driver handshake before honoring cancellation.

        Args:
            manager: Public async_playwright context manager.

        Returns:
            The initialized driver, or None after an abandoned startup is closed.
        """
        # Playwright cannot stop a pipe transport before connect has finished.
        driver = await manager.__aenter__()
        if self._abandon_start:
            await driver.stop()
            return None
        return driver

    async def _release(self) -> None:
        """Release the context and driver even if either shutdown step fails.

        Raises:
            Exception: An owned resource could not be shut down cleanly.
        """
        self._abandon_start = True
        try:
            try:
                if self.context is not None:
                    context, self.context = self.context, None
                    await asyncio.wait_for(context.close(), CLOSE_TIMEOUT_SECONDS / 2)
            finally:
                if self._startup_task is not None:
                    # Shield the handshake; a late result closes itself. On a
                    # timeout retain the task so close() can observe it again.
                    self._driver = await asyncio.wait_for(
                        asyncio.shield(self._startup_task), CLOSE_TIMEOUT_SECONDS / 2
                    )
                    self._startup_task = None
                if self._driver is not None:
                    driver, self._driver = self._driver, None
                    await asyncio.wait_for(driver.stop(), CLOSE_TIMEOUT_SECONDS / 2)
        except BaseException:
            self.status = BrowserStatus.PAUSED
            self.pause_reason = "cleanup"
            raise
        self.status = BrowserStatus.PAUSED
        self.pause_reason = "browser"
