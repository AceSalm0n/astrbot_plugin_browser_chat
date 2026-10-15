import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from browser_chat import BrowserStatus, ChatGPTBrowser


class BrowserLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_timeout_tracks_and_closes_late_driver(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        driver = AsyncMock()

        async def delayed_start():
            entered.set()
            await release.wait()
            return driver

        with tempfile.TemporaryDirectory() as directory:
            with (
                patch("playwright.async_api.async_playwright") as factory,
                patch("browser_chat.START_TIMEOUT_SECONDS", 0.02),
                patch("browser_chat.CLOSE_TIMEOUT_SECONDS", 0.02),
            ):
                factory.return_value.__aenter__ = AsyncMock(side_effect=delayed_start)
                browser = ChatGPTBrowser(Path(directory))
                task = asyncio.create_task(browser.start())
                await asyncio.wait_for(entered.wait(), 2)
                try:
                    with self.assertRaises(asyncio.TimeoutError):
                        await task
                    self.assertEqual(browser.pause_reason, "cleanup")
                    self.assertIsNotNone(browser._startup_task)
                    self.assertFalse(browser._startup_task.cancelled())
                finally:
                    release.set()
                    await browser.close()
                driver.stop.assert_awaited_once()
                driver.chromium.launch_persistent_context.assert_not_awaited()
                self.assertIsNone(browser._startup_task)

    async def test_cancel_driver_initialization_releases_manager(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        driver = AsyncMock()

        async def blocked_start():
            entered.set()
            await release.wait()
            return driver

        with tempfile.TemporaryDirectory() as directory:
            with patch("playwright.async_api.async_playwright") as factory:
                factory.return_value.__aenter__ = AsyncMock(side_effect=blocked_start)
                factory.return_value.__aexit__ = AsyncMock()
                browser = ChatGPTBrowser(Path(directory))
                task = asyncio.create_task(browser.start())
                await asyncio.wait_for(entered.wait(), 2)
                task.cancel()
                await asyncio.sleep(0)
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                driver.stop.assert_awaited_once()

    async def test_real_manager_connect_cancellation_does_not_abandon_connection(self):
        from playwright._impl._transport import PipeTransport

        entered = asyncio.Event()
        release = asyncio.Event()
        original_connect = PipeTransport.connect

        async def delayed_connect(transport):
            entered.set()
            await release.wait()
            await original_connect(transport)

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(PipeTransport, "connect", delayed_connect):
                browser = ChatGPTBrowser(Path(directory))
                task = asyncio.create_task(browser.start())
                await asyncio.wait_for(entered.wait(), 2)
                task.cancel()
                await asyncio.sleep(0.02)
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertIsNone(browser.context)
                self.assertFalse(
                    any(
                        "Connection.run" in repr(pending.get_coro())
                        for pending in asyncio.all_tasks()
                        if pending is not asyncio.current_task()
                    )
                )

    async def test_start_close_are_idempotent_and_never_claim_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            driver = AsyncMock()
            context = AsyncMock()
            driver.chromium.launch_persistent_context.return_value = context
            with patch("playwright.async_api.async_playwright") as factory:
                factory.return_value.__aenter__ = AsyncMock(return_value=driver)
                browser = ChatGPTBrowser(Path(directory))
                await browser.start()
                await browser.start()
                self.assertEqual(browser.status, BrowserStatus.PAUSED)
                self.assertEqual(browser.pause_reason, "config")
                kwargs = driver.chromium.launch_persistent_context.call_args.kwargs
                self.assertFalse(kwargs["headless"])
                self.assertEqual(kwargs["channel"], "chrome")
                self.assertEqual(
                    Path(kwargs["user_data_dir"]),
                    Path(directory).resolve() / "browser_profile",
                )
                await browser.close()
                await browser.close()
                driver.chromium.launch_persistent_context.assert_awaited_once()
                context.close.assert_awaited_once()
                driver.stop.assert_awaited_once()
                self.assertIsNone(browser.context)

    async def test_cancel_launch_releases_driver_and_can_restart(self):
        entered = asyncio.Event()

        async def blocked_launch(**kwargs):
            entered.set()
            await asyncio.Event().wait()

        with tempfile.TemporaryDirectory() as directory:
            driver = AsyncMock()
            driver.chromium.launch_persistent_context.side_effect = blocked_launch
            with patch("playwright.async_api.async_playwright") as factory:
                factory.return_value.__aenter__ = AsyncMock(return_value=driver)
                browser = ChatGPTBrowser(Path(directory))
                task = asyncio.create_task(browser.start())
                await asyncio.wait_for(entered.wait(), 2)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                driver.stop.assert_awaited_once()
                self.assertIsNone(browser.context)
                driver.chromium.launch_persistent_context.side_effect = None
                await browser.start()
                await browser.close()
                self.assertEqual(driver.stop.await_count, 2)

    async def test_failed_close_still_stops_driver(self):
        with tempfile.TemporaryDirectory() as directory:
            driver = AsyncMock()
            driver.chromium.launch_persistent_context.return_value.close.side_effect = (
                RuntimeError("close failed")
            )
            with patch("playwright.async_api.async_playwright") as factory:
                factory.return_value.__aenter__ = AsyncMock(return_value=driver)
                browser = ChatGPTBrowser(Path(directory))
                await browser.start()
                with self.assertRaises(RuntimeError):
                    await browser.close()
                driver.stop.assert_awaited_once()
                self.assertIsNone(browser.context)

    @unittest.skipUnless(
        os.environ.get("BROWSER_CHAT_REAL_TEST") == "1", "Opt-in visible Chrome test"
    )
    async def test_real_visible_chrome_local_page_and_cancellation(self):
        with tempfile.TemporaryDirectory() as directory:
            browser = ChatGPTBrowser(Path(directory))
            entered = asyncio.Event()
            owned_pages = []

            async def local_session():
                try:
                    await browser.start()
                    page = await browser.context.new_page()
                    owned_pages.append(page)
                    await page.set_content(
                        "<title>Browser Chat local probe</title><h1>Local lifecycle OK</h1>"
                    )
                    self.assertEqual(
                        await page.locator("h1").inner_text(), "Local lifecycle OK"
                    )
                    print(
                        "REAL_BROWSER",
                        browser.context.browser.version,
                        type(asyncio.get_running_loop()).__name__,
                    )
                    entered.set()
                    await asyncio.Event().wait()
                finally:
                    await browser.close()

            task = asyncio.create_task(local_session())
            try:
                await asyncio.wait_for(entered.wait(), timeout=40)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertTrue(owned_pages[0].is_closed())
                self.assertIsNone(browser.context)
                await browser.start()
                await browser.close()
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await browser.close()
