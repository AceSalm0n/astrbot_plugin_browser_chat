import asyncio
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock, patch

from browser_chat import (
    BrowserAnswer,
    BrowserError,
    BrowserStatus,
    ChatGPTBrowser,
    CleanupStatus,
)


class BrowserLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def test_unconfirmed_record_cannot_claim_conversation_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "owned_sessions.json"
            payload = {
                "version": 1,
                "sessions": {
                    "unconfirmed": {
                        "task_id": "unconfirmed",
                        "created_at": "2026-10-15T00:00:00+00:00",
                        "conversation_id": "not-confirmed",
                        "url": "https://chatgpt.com/c/not-confirmed",
                        "keep": False,
                        "answer_result": "failed",
                        "cleanup": "pending",
                        "submission": "attempted",
                    }
                },
            }
            original = json.dumps(payload)
            path.write_text(original, encoding="utf-8")
            browser = ChatGPTBrowser(Path(directory))
            self.assertEqual(browser.pause_reason, "cleanup")
            self.assertEqual(path.read_text(encoding="utf-8"), original)

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


class FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = (Path(__file__).parent / "fixtures" / "chat.html").read_text(
            encoding="utf-8"
        )
        body = body.replace("__SCENARIO_JSON__", json.dumps(self.server.scenario))
        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format, *args):
        pass


class BrowserPageTests(unittest.IsolatedAsyncioTestCase):
    async def test_visible_different_user_message_cannot_confirm_this_question(self):
        await self.ready("mismatched_user")
        with self.assertRaises(BrowserError) as raised:
            await self.browser.ask("task-user-mismatch", "Intended question", 3)
        self.assertEqual(raised.exception.category, "submission_unknown")
        self.assertIsNone(self.records()["task-user-mismatch"]["conversation_id"])
        self.assertEqual(
            (await self.browser.cleanup("task-user-mismatch")).status,
            CleanupStatus.CLEANUP_UNKNOWN,
        )

    async def test_unconfirmed_url_is_not_enough_to_claim_conversation_ownership(self):
        await self.ready("unconfirmed_with_id")
        with self.assertRaises(BrowserError) as raised:
            await self.browser.ask("task-unconfirmed-id", "Question", 2)
        self.assertEqual(raised.exception.category, "submission_unknown")
        self.assertIsNone(self.records()["task-unconfirmed-id"]["conversation_id"])
        self.assertEqual(
            (await self.browser.cleanup("task-unconfirmed-id")).status,
            CleanupStatus.CLEANUP_UNKNOWN,
        )

    async def test_security_words_in_user_text_are_not_challenge_ui(self):
        await self.ready("echo")
        question = "Explain the phrase Verify you are human"
        answer = await self.browser.ask("task-quoted-challenge", question, 3)
        self.assertEqual(answer.text, "Reply: " + question)
        await self.browser.cleanup("task-quoted-challenge")

    async def test_cleanup_write_failure_cannot_be_bypassed_by_resume(self):
        await self.ready()
        await self.browser.ask("task-cleanup-write", "Question", 3)
        with patch(
            "browser_chat.os.replace",
            side_effect=OSError("controlled replacement failure"),
        ):
            self.assertEqual(
                (await self.browser.cleanup("task-cleanup-write")).status,
                CleanupStatus.CLEANUP_UNKNOWN,
            )
            self.assertEqual(
                await self.browser.resume(account_settings_verified=True),
                BrowserStatus.PAUSED,
            )
            self.assertEqual(self.browser.pause_reason, "cleanup")
        self.assertEqual(
            await self.browser.resume(account_settings_verified=True),
            BrowserStatus.READY,
        )
        self.assertEqual(self.records()["task-cleanup-write"]["cleanup"], "deleted")

    async def test_cleanup_during_active_ask_is_busy_without_runner_deadlock(self):
        await self.ready("forever")
        final = []

        async def runner():
            try:
                await self.browser.ask("task-busy-cleanup", "Question", 5)
            finally:
                final.append(await self.browser.cleanup("task-busy-cleanup"))

        request = asyncio.create_task(runner())
        try:
            for _ in range(300):
                path = self.data_dir / "owned_sessions.json"
                if (
                    path.exists()
                    and self.records().get("task-busy-cleanup", {}).get("submission")
                    == "confirmed"
                ):
                    break
                await asyncio.sleep(0.01)
            else:
                self.fail("The controlled question did not reach confirmed submission")
            with patch("browser_chat.CLEANUP_TIMEOUT_SECONDS", 0.3):
                with self.assertRaises(BrowserError) as raised:
                    await self.browser.cleanup("task-busy-cleanup")
                self.assertEqual(raised.exception.category, "busy")
        finally:
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
        self.assertEqual(len(final), 1)
        self.assertEqual(final[0].status, CleanupStatus.DELETED)

    async def test_cancelled_cleanup_waiter_does_not_lose_owned_cleanup(self):
        await self.ready()
        await self.browser.ask("task-shared-cleanup", "Question", 3)
        first = asyncio.create_task(self.browser.cleanup("task-shared-cleanup"))
        second = asyncio.create_task(self.browser.cleanup("task-shared-cleanup"))
        await asyncio.sleep(0)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertEqual((await second).status, CleanupStatus.DELETED)
        self.assertEqual(self.records()["task-shared-cleanup"]["cleanup"], "deleted")

    async def test_close_cancels_generation_and_joins_owned_cleanup(self):
        await self.ready("forever")
        request = asyncio.create_task(self.browser.ask("task-close", "Question", 5))
        for _ in range(300):
            path = self.data_dir / "owned_sessions.json"
            if (
                path.exists()
                and self.records().get("task-close", {}).get("submission")
                == "confirmed"
            ):
                break
            await asyncio.sleep(0.01)
        else:
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
            self.fail("The controlled question did not reach confirmed submission")
        await self.browser.close()
        with self.assertRaises(asyncio.CancelledError):
            await request
        self.assertIsNone(self.browser.context)
        record = self.records()["task-close"]
        self.assertEqual(record["answer_result"], "cancelled")
        self.assertEqual(record["cleanup"], "deleted")

    async def test_late_page_creation_is_tracked_until_it_can_be_closed(self):
        await self.ready()
        context = self.browser.context
        original = context.new_page
        entered = asyncio.Event()
        release = asyncio.Event()

        async def delayed_open():
            entered.set()
            await release.wait()
            return await original()

        with patch.object(context, "new_page", new=delayed_open):
            request = asyncio.create_task(
                self.browser.ask("task-late-page", "Question", 0.1)
            )
            await asyncio.wait_for(entered.wait(), 1)
            with self.assertRaises(BrowserError):
                await request
            with patch("browser_chat.CLEANUP_TIMEOUT_SECONDS", 0.15):
                cleaned = await asyncio.wait_for(
                    self.browser.cleanup("task-late-page"), 1
                )
            self.assertIn(
                cleaned.status,
                (CleanupStatus.CLEANUP_FAILED, CleanupStatus.CLEANUP_UNKNOWN),
            )
            try:
                self.assertNotEqual(
                    await self.browser.resume(account_settings_verified=True),
                    BrowserStatus.READY,
                )
            except BrowserError as error:
                self.assertEqual(error.category, "busy")
            release.set()
            await asyncio.sleep(0.1)
        await self.browser.close()
        self.assertIsNone(self.browser.context)

    async def test_unknown_cleanup_before_admission_cannot_poison_later_cleanup(self):
        await self.ready()
        self.assertEqual(
            (await self.browser.cleanup("future-task")).status,
            CleanupStatus.NOT_CREATED,
        )
        await self.browser.ask("future-task", "Question", 3)
        self.assertEqual(
            (await self.browser.cleanup("future-task")).status, CleanupStatus.DELETED
        )

    async def test_region_error_keeps_resume_paused(self):
        self.server.scenario = "unsupported_region"
        self.assertEqual(
            await self.browser.resume(account_settings_verified=True),
            BrowserStatus.PAUSED,
        )
        self.assertEqual(self.browser.pause_reason, "config")

    async def test_failed_atomic_replace_preserves_previous_complete_registry(self):
        await self.ready()
        await self.browser.ask("task-before-write", "First question", 3, keep=True)
        await self.browser.cleanup("task-before-write")
        path = self.data_dir / "owned_sessions.json"
        before = path.read_bytes()
        with patch(
            "browser_chat.os.replace",
            side_effect=OSError("controlled replacement failure"),
        ):
            with self.assertRaises(BrowserError):
                await self.browser.ask("task-failed-write", "Second question", 3)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(await self.submission_count(), 1)

    async def test_only_registered_conversation_is_deleted(self):
        await self.ready()
        unrelated = await self.browser.context.new_page()
        await unrelated.goto(self.base_url, wait_until="domcontentloaded")
        await unrelated.locator("#prompt-textarea").fill("Unrelated local conversation")
        await unrelated.locator('[data-testid="send-button"]').click()
        await unrelated.locator('[data-testid="copy-turn-action-button"]').wait_for()
        unrelated_id = unrelated.url.rsplit("/", 1)[-1]
        await unrelated.close()
        answer = await self.browser.ask("task-owned", "Owned question", 3)
        self.assertNotEqual(unrelated_id, answer.conversation_id)
        self.assertEqual(
            (await self.browser.cleanup("task-owned")).status, CleanupStatus.DELETED
        )
        check = await self.browser.context.new_page()
        try:
            await check.goto(self.base_url, wait_until="domcontentloaded")
            self.assertTrue(
                await check.locator(f'a[href="/c/{unrelated_id}"]').is_visible()
            )
            self.assertEqual(
                await check.locator(f'a[href="/c/{answer.conversation_id}"]').count(), 0
            )
        finally:
            await check.close()

    async def test_concurrent_pages_do_not_exchange_answers_or_cleanup_targets(self):
        await self.ready("echo")
        first, second = await asyncio.gather(
            self.browser.ask("task-first", "First question", 6),
            self.browser.ask("task-second", "Second question", 6),
        )
        self.assertEqual(first.text, "Reply: First question")
        self.assertEqual(second.text, "Reply: Second question")
        self.assertNotEqual(first.conversation_id, second.conversation_id)
        results = await asyncio.gather(
            self.browser.cleanup("task-first"), self.browser.cleanup("task-second")
        )
        self.assertTrue(all(item.status == CleanupStatus.DELETED for item in results))

    async def test_restart_deletes_known_nonretained_residual(self):
        await self.ready()
        answer = await self.browser.ask("task-residual", "Question", 3, keep=False)
        await self.browser.context.close()
        await self.browser.close()
        self.browser = ChatGPTBrowser(self.data_dir, base_url=self.base_url)
        await self.browser.start()
        self.assertEqual(
            await self.browser.resume(account_settings_verified=True),
            BrowserStatus.READY,
        )
        record = self.records()["task-residual"]
        self.assertEqual(record["conversation_id"], answer.conversation_id)
        self.assertEqual(record["cleanup"], "deleted")

    async def test_invalid_foreign_record_url_is_never_navigated(self):
        await self.ready()
        await self.browser.ask("task-tampered", "Question", 3, keep=True)
        await self.browser.cleanup("task-tampered")
        await self.browser.close()
        path = self.data_dir / "owned_sessions.json"
        records = self.records()
        records["task-tampered"]["url"] = "https://example.com/c/not-owned"
        changed = json.dumps({"version": 1, "sessions": records})
        path.write_text(changed, encoding="utf-8")
        try:
            self.browser = ChatGPTBrowser(self.data_dir, base_url=self.base_url)
            await self.browser.start()
            self.assertEqual(
                await self.browser.resume(account_settings_verified=True),
                BrowserStatus.PAUSED,
            )
            self.assertTrue(
                all(
                    "example.com" not in page.url for page in self.browser.context.pages
                )
            )
        except BrowserError:
            pass
        self.assertEqual(path.read_text(encoding="utf-8"), changed)

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        cls.server.scenario = "stream"
        cls.server_thread = threading.Thread(
            target=cls.server.serve_forever, daemon=True
        )
        cls.server_thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}/"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=2)

    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.directory.name)
        self.server.scenario = "stream"
        self.stable_patch = patch(
            "browser_chat.STABLE_WINDOW_SECONDS", 0.05, create=True
        )
        self.poll_patch = patch("browser_chat.POLL_INTERVAL_SECONDS", 0.01, create=True)
        self.stable_patch.start()
        self.poll_patch.start()
        self.browser = ChatGPTBrowser(self.data_dir, base_url=self.base_url)
        await self.browser.start()

    async def asyncTearDown(self):
        try:
            await self.browser.close()
        finally:
            self.stable_patch.stop()
            self.poll_patch.stop()
            self.directory.cleanup()

    async def ready(self, scenario="stream"):
        self.server.scenario = scenario
        status = await self.browser.resume(account_settings_verified=True)
        self.assertEqual(status, BrowserStatus.READY)

    def records(self):
        return json.loads(
            (self.data_dir / "owned_sessions.json").read_text(encoding="utf-8")
        )["sessions"]

    async def submission_count(self):
        page = await self.browser.context.new_page()
        try:
            await page.goto(self.base_url, wait_until="domcontentloaded")
            return int(await page.locator("#submission-count").inner_text())
        finally:
            await page.close()

    async def test_streamed_answer_and_exact_owned_deletion(self):
        await self.ready()
        answer = await self.browser.ask("task-stream", "A private fixture question", 3)
        self.assertIsInstance(answer, BrowserAnswer)
        self.assertEqual(answer.text, "Fixture final answer")
        self.assertTrue(answer.conversation_id)
        record = self.records()["task-stream"]
        self.assertEqual(record["conversation_id"], answer.conversation_id)
        self.assertFalse(record["keep"])
        self.assertEqual(record["answer_result"], "success")
        serialized = (self.data_dir / "owned_sessions.json").read_text(encoding="utf-8")
        self.assertNotIn("A private fixture question", serialized)
        self.assertNotIn("Fixture final answer", serialized)
        cleaned = await self.browser.cleanup("task-stream", keep=False)
        self.assertEqual(cleaned.status, CleanupStatus.DELETED)
        self.assertEqual(
            (await self.browser.cleanup("task-stream")).status, CleanupStatus.DELETED
        )
        self.assertEqual(await self.submission_count(), 1)

    async def test_long_pause_is_not_mistaken_for_completion(self):
        await self.ready("long_pause")
        before = asyncio.get_running_loop().time()
        answer = await self.browser.ask("task-pause", "Question", 3)
        self.assertEqual(answer.text, "prefix then final content")
        self.assertGreaterEqual(asyncio.get_running_loop().time() - before, 0.2)
        await self.browser.cleanup("task-pause")

    async def test_long_stable_prefix_requires_final_generation_state(self):
        await self.ready("long_prefix")
        answer = await self.browser.ask("task-prefix", "Question", 3)
        self.assertEqual(answer.text, "x" * 13000 + " FINAL TAIL")
        await self.browser.cleanup("task-prefix")

    async def test_code_and_visible_citation_link_are_retained(self):
        await self.ready("code_links")
        answer = await self.browser.ask("task-code", "Question", 3)
        self.assertIn('```python\nprint("fixture")\n```', answer.text)
        self.assertIn("https://example.com/reference", answer.text)
        await self.browser.cleanup("task-code")

    async def test_empty_answer_never_becomes_success(self):
        await self.ready("empty")
        with self.assertRaises(BrowserError) as raised:
            await self.browser.ask("task-empty", "Question", 2)
        self.assertIn(raised.exception.category, ("timeout", "empty_answer"))
        self.assertEqual(self.records()["task-empty"]["answer_result"], "timed_out")
        self.assertEqual(self.records()["task-empty"]["submission"], "confirmed")
        await self.browser.cleanup("task-empty")

    async def test_unconfirmed_submission_is_never_retried(self):
        await self.ready("unconfirmed")
        with self.assertRaises(BrowserError) as raised:
            await self.browser.ask("task-unknown", "Question", 2)
        self.assertEqual(raised.exception.category, "submission_unknown")
        self.assertTrue(raised.exception.submission_unknown)
        self.assertEqual(await self.submission_count(), 1)
        with self.assertRaises(BrowserError):
            await self.browser.ask("task-unknown", "Question", 1)
        self.assertEqual(await self.submission_count(), 1)
        self.assertEqual(
            (await self.browser.cleanup("task-unknown")).status,
            CleanupStatus.CLEANUP_UNKNOWN,
        )

    async def test_error_after_click_before_confirmation_is_unknown(self):
        await self.ready("submit_error")
        with self.assertRaises(BrowserError) as raised:
            await self.browser.ask("task-submit-error", "Question", 3)
        self.assertEqual(raised.exception.category, "submission_unknown")
        self.assertEqual(await self.submission_count(), 1)
        await self.browser.cleanup("task-submit-error")

    async def test_generation_timeout_does_not_return_a_stable_partial_answer(self):
        await self.ready("forever")
        with self.assertRaises(BrowserError) as raised:
            await self.browser.ask("task-timeout", "Question", 2)
        self.assertEqual(raised.exception.category, "timeout")
        self.assertEqual(self.records()["task-timeout"]["answer_result"], "timed_out")
        self.assertEqual(self.records()["task-timeout"]["submission"], "confirmed")
        await self.browser.cleanup("task-timeout")

    async def test_cancel_during_generation_propagates_and_remains_cleanable(self):
        await self.ready("forever")
        request = asyncio.create_task(self.browser.ask("task-cancel", "Question", 3))
        for _ in range(200):
            record = (
                self.records().get("task-cancel")
                if (self.data_dir / "owned_sessions.json").exists()
                else None
            )
            if record and record.get("submission") == "confirmed":
                break
            await asyncio.sleep(0.01)
        else:
            self.fail("The controlled question never reached confirmed submission")
        request.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await request
        self.assertEqual(self.records()["task-cancel"]["answer_result"], "cancelled")
        self.assertEqual(
            (await self.browser.cleanup("task-cancel")).status, CleanupStatus.DELETED
        )

    async def test_old_answer_cannot_satisfy_a_new_request(self):
        await self.ready("old_answer")
        with self.assertRaises(BrowserError):
            await self.browser.ask("task-old", "New question", 2)
        self.assertNotEqual(
            self.records().get("task-old", {}).get("answer_result"), "success"
        )
        await self.browser.cleanup("task-old")

    async def test_login_page_keeps_resume_paused(self):
        self.server.scenario = "login"
        self.assertEqual(
            await self.browser.resume(account_settings_verified=True),
            BrowserStatus.PAUSED,
        )
        self.assertEqual(self.browser.pause_reason, "login")

    async def test_challenge_page_keeps_resume_paused(self):
        self.server.scenario = "challenge"
        self.assertEqual(
            await self.browser.resume(account_settings_verified=True),
            BrowserStatus.PAUSED,
        )
        self.assertEqual(self.browser.pause_reason, "challenge")

    async def test_changed_structure_keeps_resume_paused(self):
        self.server.scenario = "changed"
        self.assertEqual(
            await self.browser.resume(account_settings_verified=True),
            BrowserStatus.PAUSED,
        )
        self.assertEqual(self.browser.pause_reason, "browser")

    async def test_unverified_account_settings_never_become_ready(self):
        self.assertEqual(await self.browser.resume(), BrowserStatus.PAUSED)
        self.assertEqual(self.browser.pause_reason, "config")

    async def test_rate_limit_is_classified_and_pauses(self):
        await self.ready("rate_limit")
        with self.assertRaises(BrowserError) as raised:
            await self.browser.ask("task-limit", "Question", 3)
        self.assertEqual(raised.exception.category, "rate_limit")
        self.assertEqual(self.browser.pause_reason, "rate_limit")
        await self.browser.cleanup("task-limit")

    async def test_login_loss_during_generation_is_classified(self):
        await self.ready("login_expired")
        with self.assertRaises(BrowserError) as raised:
            await self.browser.ask("task-login", "Question", 3)
        self.assertEqual(raised.exception.category, "login")
        self.assertEqual(self.browser.pause_reason, "login")
        await self.browser.cleanup("task-login")

    async def test_completion_control_missing_cannot_report_success(self):
        await self.ready("missing_completion")
        with self.assertRaises(BrowserError):
            await self.browser.ask("task-structure", "Question", 2)
        await self.browser.cleanup("task-structure")

    async def test_cleanup_failure_does_not_change_answer_success(self):
        await self.ready("delete_failure")
        answer = await self.browser.ask("task-delete-fail", "Question", 3)
        cleaned = await self.browser.cleanup("task-delete-fail")
        self.assertEqual(cleaned.status, CleanupStatus.CLEANUP_FAILED)
        self.assertEqual(self.records()["task-delete-fail"]["answer_result"], "success")
        self.assertEqual(answer.text, "Fixture final answer")
        self.assertEqual(self.browser.pause_reason, "cleanup")

    async def test_cleanup_unknown_does_not_claim_deletion(self):
        await self.ready("delete_unknown")
        await self.browser.ask("task-delete-unknown", "Question", 3)
        with patch("browser_chat.CLEANUP_TIMEOUT_SECONDS", 0.2, create=True):
            cleaned = await self.browser.cleanup("task-delete-unknown")
        self.assertEqual(cleaned.status, CleanupStatus.CLEANUP_UNKNOWN)
        self.assertEqual(
            self.records()["task-delete-unknown"]["answer_result"], "success"
        )

    async def test_retention_is_snapshotted_before_submission(self):
        await self.ready()
        await self.browser.ask("task-kept", "Question", 3, keep=True)
        self.assertTrue(self.records()["task-kept"]["keep"])
        self.assertEqual(
            (await self.browser.cleanup("task-kept", keep=False)).status,
            CleanupStatus.RETAINED,
        )

    async def test_unknown_conversation_id_is_not_guessed_from_history(self):
        await self.ready("unknown_id")
        answer = await self.browser.ask("task-no-id", "Question", 3)
        self.assertIsNone(answer.conversation_id)
        self.assertEqual(
            (await self.browser.cleanup("task-no-id")).status,
            CleanupStatus.CLEANUP_UNKNOWN,
        )
        self.assertEqual(
            (await self.browser.cleanup("unowned-task")).status,
            CleanupStatus.NOT_CREATED,
        )
        self.assertEqual(self.browser.pause_reason, "cleanup")
        self.assertEqual(
            await self.browser.resume(account_settings_verified=True),
            BrowserStatus.PAUSED,
        )
        self.assertEqual(self.browser.pause_reason, "cleanup")

    async def test_restart_recovers_only_known_records_and_original_policy(self):
        await self.ready()
        await self.browser.ask("task-restart-keep", "Question", 3, keep=True)
        await self.browser.cleanup("task-restart-keep")
        await self.browser.close()
        records = self.records()
        records["task-restart-keep"]["cleanup"] = "cleanup_failed"
        (self.data_dir / "owned_sessions.json").write_text(
            json.dumps({"version": 1, "sessions": records}), encoding="utf-8"
        )
        self.browser = ChatGPTBrowser(self.data_dir, base_url=self.base_url)
        await self.browser.start()
        self.assertEqual(
            await self.browser.resume(account_settings_verified=True),
            BrowserStatus.READY,
        )
        self.assertEqual(self.records()["task-restart-keep"]["cleanup"], "retained")

    async def test_atomic_write_failure_prevents_question_submission(self):
        await self.ready()
        with patch(
            "browser_chat.os.replace",
            side_effect=OSError("controlled replacement failure"),
        ):
            with self.assertRaises(BrowserError):
                await self.browser.ask("task-atomic", "Question", 1)
        self.assertEqual(await self.submission_count(), 0)
        path = self.data_dir / "owned_sessions.json"
        if path.exists():
            json.loads(path.read_text(encoding="utf-8"))

    async def test_corrupt_records_are_not_overwritten_or_used_for_deletion(self):
        await self.browser.close()
        path = self.data_dir / "owned_sessions.json"
        path.write_text("{not valid json", encoding="utf-8")
        try:
            self.browser = ChatGPTBrowser(self.data_dir, base_url=self.base_url)
            await self.browser.start()
            self.assertEqual(
                await self.browser.resume(account_settings_verified=True),
                BrowserStatus.PAUSED,
            )
        except BrowserError:
            pass
        self.assertEqual(path.read_text(encoding="utf-8"), "{not valid json")
