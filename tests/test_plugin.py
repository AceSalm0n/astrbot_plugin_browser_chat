"""Controlled integration tests; no host, browser, or QQ connection is started."""

import asyncio
import importlib.util
import inspect
import json
import logging
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch


def load_plugin():
    root = Path(__file__).resolve().parents[1]
    package = ModuleType("browser_chat_integration_test")
    package.__path__ = [str(root)]
    sys.modules[package.__name__] = package
    api = ModuleType("astrbot.api")
    api.AstrBotConfig = dict
    event_api = ModuleType("astrbot.api.event")
    event_api.AstrMessageEvent = object

    def regex(pattern):
        def decorate(handler):
            handler.pattern = re.compile(pattern)
            return handler

        return decorate

    event_api.filter = SimpleNamespace(regex=regex)
    star_api = ModuleType("astrbot.api.star")

    class Star:
        def __init__(self, context, config=None):
            self.context = context
            self.logger = logging.getLogger("plugin_test")

    star_api.Star = Star
    star_api.Context = object
    star_api.StarTools = SimpleNamespace(get_data_dir=lambda name: root / "data" / name)
    name = package.__name__ + ".main"
    spec = importlib.util.spec_from_file_location(name, root / "main.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    with patch.dict(
        sys.modules,
        {
            "astrbot": ModuleType("astrbot"),
            "astrbot.api": api,
            "astrbot.api.event": event_api,
            "astrbot.api.star": star_api,
        },
    ):
        spec.loader.exec_module(module)
    return module


m = load_plugin()


class Event:
    def __init__(
        self,
        text="/ask ai question",
        *,
        sender="10001",
        group="20001",
        message_id="1",
        admin=False,
        awake=False,
        client=None,
    ):
        self.message_str = text
        self.messages = [SimpleNamespace(type="Plain", text=text)]
        self.sender, self.group, self.admin = sender, group, admin
        self.is_at_or_wake_command = awake
        self.bot = client or SimpleNamespace(
            call_action=AsyncMock(return_value={"message_id": 9})
        )
        self.message_obj = SimpleNamespace(message_id=message_id)
        self.stopped = False
        self.sent = []
        self.unified_msg_origin = "bot-a:GroupMessage:" + group

    def get_message_str(self):
        return self.message_str

    def get_messages(self):
        return self.messages

    def get_sender_id(self):
        return self.sender

    def get_group_id(self):
        return self.group

    def get_platform_id(self):
        return "bot-a"

    def get_platform_name(self):
        return "aiocqhttp"

    def get_self_id(self):
        return "90001"

    def is_admin(self):
        return self.admin

    def stop_event(self):
        self.stopped = True

    def plain_result(self, text):
        return text

    async def send(self, result):
        self.sent.append(result)
        return None


class Browser:
    def __init__(self):
        self.status, self.pause_reason = m.BrowserStatus.PAUSED, "config"
        self.context = None
        self.ask = AsyncMock(return_value=m.BrowserAnswer("answer", "owned"))
        self.cleanup = AsyncMock(
            side_effect=lambda task_id: m.CleanupResult(
                m.CleanupStatus.DELETED, task_id
            )
        )
        self.start = AsyncMock(side_effect=self._start)
        self.resume = AsyncMock(side_effect=self._resume)
        self.close = AsyncMock()

    async def _start(self):
        self.context = object()

    async def _resume(self, *, account_settings_verified=False):
        if account_settings_verified:
            self.status, self.pause_reason = m.BrowserStatus.READY, None
        return self.status


class ConfigTests(unittest.TestCase):
    def test_schema_defaults_match_validation_and_lifecycle_is_async(self):
        schema = json.loads(
            (Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(
                encoding="utf-8"
            )
        )
        defaults = {key: item["default"] for key, item in schema.items()}
        self.assertEqual(m.validate_config(defaults), m.validate_config({}))
        self.assertNotIn("__del__", m.BrowserChatPlugin.__dict__)
        self.assertTrue(inspect.iscoroutinefunction(m.BrowserChatPlugin.terminate))
        self.assertEqual(defaults["max_concurrency"], 1)
        self.assertEqual(defaults["queue_capacity"], 5)

    def test_invalid_config_is_rejected_without_sensitive_value_in_error(self):
        invalid = {
            "max_concurrency": [0, True],
            "queue_capacity": [-1, False],
            "execution_timeout_seconds": [float("nan"), float("inf"), 0, True, 10**400],
            "queue_timeout_seconds": [-1],
            "max_input_chars": [0],
            "max_reply_chars": [1],
            "forward_node_chars": [0, 12001],
            "allowed_users": [[" 123"], ["0"], [123]],
            "admin_ids": [["secret-token"]],
            "restrict_groups": ["false"],
            "keep_conversations": [1],
            "browser_channel": ["unknown"],
            "user_failure_template": ["{reason.__class__}"],
            "admin_failure_template": ["{unknown}"],
        }
        for key, values in invalid.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    with self.assertRaises(m.ConfigError) as caught:
                        m.validate_config({key: value})
                    self.assertNotIn("secret", str(caught.exception))


class PluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.browser = Browser()
        self.factory = patch.object(m, "ChatGPTBrowser", return_value=self.browser)
        self.factory.start()
        self.directory = patch.object(
            m.StarTools, "get_data_dir", return_value=Path(self.temp.name)
        )
        self.directory.start()
        self.context = SimpleNamespace(
            get_config=lambda **kwargs: {"wake_prefix": ["#"]},
            platform_manager=SimpleNamespace(get_insts=lambda: []),
        )
        self.plugin = m.BrowserChatPlugin(self.context, {})
        await self.plugin.initialize()

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.directory.stop()
        self.factory.stop()
        self.temp.cleanup()

    async def ready(self):
        await self.plugin.on_command(Event("/ask resume 已核验", admin=True))
        self.assertEqual(self.plugin.manager.snapshot().state, m.ManagerState.RUNNING)

    async def drain(self):
        await asyncio.wait_for(asyncio.gather(*tuple(self.plugin._watchers)), 3)

    async def test_initialization_stays_paused_and_admin_permission_is_independent(
        self,
    ):
        self.assertEqual(self.plugin.manager.snapshot().state, m.ManagerState.PAUSED)
        self.browser.start.assert_awaited_once()
        self.browser.resume.assert_awaited_once_with()
        for text in ("/ask status", "/ask resume 已核验"):
            event = Event(text)
            await self.plugin.on_command(event)
            self.assertTrue(event.stopped)
            self.assertIn("权限", "".join(event.sent))
        self.assertEqual(self.browser.resume.await_count, 1)

    async def test_exact_command_boundaries_and_wake_prefix_plain_normalization(self):
        await self.ready()
        for text in (
            "/ask aibot question",
            "/ask status extra",
            "/ask cancel 10002",
            "ask ai question",
        ):
            event = Event(text)
            await self.plugin.on_command(event)
            self.assertFalse(event.stopped)
        event = Event("#ask ai original\ntext", awake=True)
        event.message_str = "ask ai original\ntext"
        await self.plugin.on_command(event)
        await self.drain()
        self.assertTrue(event.stopped)
        self.assertEqual(self.browser.ask.call_args.args[1], "original\ntext")
        self.assertFalse(inspect.isasyncgenfunction(self.plugin.on_command))

    async def test_allowlist_denial_does_not_call_browser(self):
        await self.plugin.terminate()
        self.plugin = m.BrowserChatPlugin(self.context, {"restrict_users": True})
        await self.plugin.initialize()
        event = Event()
        await self.plugin.on_command(event)
        self.browser.ask.assert_not_called()
        self.assertTrue(event.stopped)
        self.assertIn("权限", "".join(event.sent))

    async def test_one_accepted_job_reference_snapshot_and_duplicate_message(self):
        await self.ready()
        event = Event()
        event.messages.append(
            SimpleNamespace(
                type="Reply", chain=[SimpleNamespace(type="Plain", text="quoted")]
            )
        )
        await self.plugin.on_command(event)
        await self.drain()
        await self.plugin.on_command(event)
        self.browser.ask.assert_awaited_once()
        self.assertIn("quoted", self.browser.ask.call_args.args[1])
        self.browser.cleanup.assert_awaited_once()
        forward = [
            call
            for call in event.bot.call_action.call_args_list
            if call.args[0].endswith("forward_msg")
        ]
        self.assertEqual(len(forward), 1)
        self.assertTrue(event.stopped)

    async def test_cleanup_holds_slot_and_success_survives_terminate(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def cleanup(task_id):
            entered.set()
            await release.wait()
            return m.CleanupResult(m.CleanupStatus.DELETED, task_id)

        self.browser.cleanup.side_effect = cleanup
        await self.ready()
        event = Event()
        await self.plugin.on_command(event)
        await entered.wait()
        self.assertEqual(self.plugin.manager.snapshot().running, 1)
        task_id = self.browser.ask.call_args.args[0]
        closing = asyncio.create_task(self.plugin.terminate())
        await asyncio.sleep(0)
        self.assertFalse(closing.done())
        self.browser.close.assert_not_called()
        release.set()
        await closing
        self.assertEqual(
            (await self.plugin.manager.wait(task_id)).result, m.JobResult.SUCCESS
        )
        self.browser.cleanup.assert_awaited_once()
        self.browser.close.assert_awaited_once()

    async def test_execution_timeout_has_no_late_delivery_and_still_cleans(self):
        await self.plugin.terminate()
        self.plugin = m.BrowserChatPlugin(
            self.context, {"execution_timeout_seconds": 0.02}
        )
        await self.plugin.initialize()

        async def blocked(*args, **kwargs):
            await asyncio.Event().wait()

        self.browser.ask.side_effect = blocked
        await self.ready()
        event = Event()
        await self.plugin.on_command(event)
        await self.drain()
        task_id = self.browser.ask.call_args.args[0]
        outcome = await self.plugin.manager.wait(task_id)
        self.assertEqual(outcome.result, m.JobResult.TIMED_OUT)
        self.browser.cleanup.assert_awaited_once()
        self.assertFalse(
            any(
                call.args[0].endswith("forward_msg")
                for call in event.bot.call_action.call_args_list
            )
        )

    async def test_cancel_own_cross_group_task_waits_for_cleanup(self):
        entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def ask(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        async def cleanup(task_id):
            cleaning.set()
            await release.wait()
            return m.CleanupResult(m.CleanupStatus.DELETED, task_id)

        self.browser.ask.side_effect, self.browser.cleanup.side_effect = ask, cleanup
        await self.ready()
        await self.plugin.on_command(Event())
        await entered.wait()
        cancel = Event("/ask cancel", group="30001", message_id="2")
        cancelling = asyncio.create_task(self.plugin.on_command(cancel))
        await cleaning.wait()
        self.assertEqual(self.plugin.manager.snapshot().running, 1)
        self.assertFalse(cancelling.done())
        release.set()
        await cancelling
        await self.drain()
        self.assertIn("不会撤回", "".join(cancel.sent))

    async def test_cancellation_in_delivery_is_unknown_and_never_full_retry(self):
        submitted = asyncio.Event()

        async def action(name, **params):
            if name.endswith("forward_msg"):
                submitted.set()
                await asyncio.Event().wait()
            return {"message_id": 3}

        await self.ready()
        event = Event(client=SimpleNamespace(call_action=AsyncMock(side_effect=action)))
        await self.plugin.on_command(event)
        await submitted.wait()
        task_id = self.browser.ask.call_args.args[0]
        await self.plugin.manager.cancel(("bot-a", "10001"))
        await self.drain()
        self.assertEqual(
            (await self.plugin.manager.wait(task_id)).result,
            m.JobResult.DELIVERY_UNKNOWN,
        )
        self.assertEqual(
            sum(
                call.args[0].endswith("forward_msg")
                for call in event.bot.call_action.call_args_list
            ),
            1,
        )

    async def test_pause_inside_ask_before_delivery_does_not_deadlock_or_send_answer(
        self,
    ):
        async def ask(*args, **kwargs):
            await self.plugin.manager.pause("login")
            return m.BrowserAnswer("must not be sent", "owned")

        self.browser.ask.side_effect = ask
        await self.ready()
        event = Event()
        await self.plugin.on_command(event)
        await self.drain()
        self.assertFalse(
            any(
                call.args[0].endswith("forward_msg")
                for call in event.bot.call_action.call_args_list
            )
        )
        self.assertEqual(self.plugin.manager.snapshot().state, m.ManagerState.PAUSED)

    async def test_login_fault_pauses_waiting_once_and_resume_requires_drained_browser(
        self,
    ):
        entered, fail, cleaning, release = [asyncio.Event() for _ in range(4)]

        async def ask(*args, **kwargs):
            entered.set()
            await fail.wait()
            self.browser.status, self.browser.pause_reason = (
                m.BrowserStatus.PAUSED,
                "login",
            )
            raise m.BrowserError("login")

        async def cleanup(task_id):
            cleaning.set()
            await release.wait()
            return m.CleanupResult(m.CleanupStatus.DELETED, task_id)

        self.browser.ask.side_effect, self.browser.cleanup.side_effect = ask, cleanup
        await self.ready()
        first, queued = Event(), Event(sender="10002", message_id="2")
        await self.plugin.on_command(first)
        await entered.wait()
        await self.plugin.on_command(queued)
        fail.set()
        await cleaning.wait()
        resumed = self.browser.resume.await_count
        await self.plugin.on_command(Event("/ask resume 已核验", admin=True))
        self.assertEqual(self.browser.resume.await_count, resumed)
        release.set()
        await self.drain()
        self.assertEqual(self.browser.ask.await_count, 1)
        self.assertEqual(queued.bot.call_action.await_count, 1)
        await self.plugin.on_command(Event("/ask resume 已核验", admin=True))
        self.assertEqual(self.plugin.manager.snapshot().state, m.ManagerState.RUNNING)

    async def test_cleanup_failure_does_not_overwrite_success(self):
        self.browser.cleanup.side_effect = lambda task_id: m.CleanupResult(
            m.CleanupStatus.CLEANUP_FAILED, task_id
        )
        await self.ready()
        await self.plugin.on_command(Event())
        await self.drain()
        outcome = await self.plugin.manager.wait(self.browser.ask.call_args.args[0])
        self.assertEqual(outcome.result, m.JobResult.SUCCESS)
        self.assertEqual(outcome.cleanup, m.CleanupState.CLEANUP_FAILED)
        self.assertEqual(self.plugin.manager.snapshot().state, m.ManagerState.PAUSED)

    async def test_missing_dependency_and_invalid_config_pause_without_install(self):
        await self.plugin.terminate()
        self.browser.start.side_effect = ModuleNotFoundError("playwright")
        self.plugin = m.BrowserChatPlugin(self.context, {})
        await self.plugin.initialize()
        self.assertEqual(self.plugin.manager.snapshot().pause_reason, "dependency")
        await self.plugin.terminate()
        self.plugin = m.BrowserChatPlugin(self.context, {"queue_capacity": -1})
        await self.plugin.initialize()
        self.assertEqual(self.plugin.manager.snapshot().pause_reason, "config")
        self.assertIn("queue_capacity", self.plugin.config_error)

    async def test_reload_cancels_old_generation_and_new_instance_uses_new_snapshot(
        self,
    ):
        entered = asyncio.Event()

        async def ask(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        self.browser.ask.side_effect = ask
        await self.ready()
        await self.plugin.on_command(Event())
        await entered.wait()
        await self.plugin.terminate()
        self.assertEqual(self.plugin.manager.snapshot().state, m.ManagerState.CLOSED)
        self.assertFalse(self.plugin._watchers)
        self.plugin = m.BrowserChatPlugin(
            self.context, {"queue_capacity": 0, "keep_conversations": True}
        )
        self.browser.ask.side_effect = None
        await self.plugin.initialize()
        await self.ready()
        await self.plugin.on_command(Event(message_id="new"))
        await self.drain()
        self.assertTrue(self.browser.ask.call_args.kwargs["keep"])

    async def test_initialize_terminate_are_idempotent_and_shared(self):
        await asyncio.gather(self.plugin.initialize(), self.plugin.initialize())
        self.browser.start.assert_awaited_once()
        await asyncio.gather(self.plugin.terminate(), self.plugin.terminate())
        self.browser.close.assert_awaited_once()

    async def test_admin_status_is_safe_and_foreign_platform_cannot_spoof_plugin_admin(
        self,
    ):
        status = Event("/ask status", admin=True)
        await self.plugin.on_command(status)
        self.assertIn("等待 0", "".join(status.sent))
        foreign = Event("/ask resume 已核验", admin=True)
        foreign.get_platform_name = lambda: "webchat"
        before = self.browser.resume.await_count
        await self.plugin.on_command(foreign)
        self.assertEqual(self.browser.resume.await_count, before)

    async def test_execution_deadline_rejects_answer_returned_after_timeout_cancellation(
        self,
    ):
        await self.plugin.terminate()
        self.plugin = m.BrowserChatPlugin(
            self.context, {"execution_timeout_seconds": 0.02}
        )
        await self.plugin.initialize()

        async def late(*args, **kwargs):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return m.BrowserAnswer("late answer", "owned")

        self.browser.ask.side_effect = late
        await self.ready()
        event = Event()
        await self.plugin.on_command(event)
        await self.drain()
        self.assertFalse(
            any(
                call.args[0].endswith("forward_msg")
                for call in event.bot.call_action.call_args_list
            )
        )
        self.assertEqual(
            (await self.plugin.manager.wait(self.browser.ask.call_args.args[0])).result,
            m.JobResult.TIMED_OUT,
        )

    async def test_startup_binding_uses_public_login_info_and_plugin_admin_is_independent(
        self,
    ):
        await self.plugin.terminate()
        client = SimpleNamespace(call_action=AsyncMock(return_value={"user_id": 90001}))
        platform = SimpleNamespace(
            meta=lambda: SimpleNamespace(name="aiocqhttp", id="bot-a"),
            get_client=lambda: client,
        )
        self.context.platform_manager.get_insts = lambda: [platform]
        self.plugin = m.BrowserChatPlugin(
            self.context, {"admin_ids": ["10005"], "restrict_users": True}
        )
        await self.plugin.initialize()
        client.call_action.assert_awaited_once_with("get_login_info")
        admin = Event("/ask resume 已核验", sender="10005", client=client)
        await self.plugin.on_command(admin)
        self.assertEqual(self.plugin.manager.snapshot().state, m.ManagerState.RUNNING)
        denied = Event(sender="10005", client=client)
        await self.plugin.on_command(denied)
        self.browser.ask.assert_not_called()

    async def test_terminate_during_initialize_never_reopens_admission(self):
        await self.plugin.terminate()
        entered, release = asyncio.Event(), asyncio.Event()

        async def start():
            entered.set()
            await release.wait()
            self.browser.context = object()

        self.browser.start.side_effect = start
        self.browser.close.reset_mock()
        self.browser.resume.reset_mock()
        self.plugin = m.BrowserChatPlugin(self.context, {})
        starting = asyncio.create_task(self.plugin.initialize())
        await entered.wait()
        closing = asyncio.create_task(self.plugin.terminate())
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(starting, closing)
        self.assertEqual(self.plugin.manager.snapshot().state, m.ManagerState.CLOSED)
        self.browser.close.assert_awaited_once()
        self.browser.resume.assert_not_called()

    async def test_queued_timeout_reports_once_without_running_browser(self):
        await self.plugin.terminate()
        self.plugin = m.BrowserChatPlugin(self.context, {"queue_timeout_seconds": 0.02})
        await self.plugin.initialize()
        entered, noticed = asyncio.Event(), asyncio.Event()

        async def ask(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        async def notice(*args, **kwargs):
            noticed.set()
            return {"message_id": 8}

        self.browser.ask.side_effect = ask
        await self.ready()
        await self.plugin.on_command(Event())
        await entered.wait()
        queued = Event(
            sender="10002",
            message_id="2",
            client=SimpleNamespace(call_action=AsyncMock(side_effect=notice)),
        )
        await self.plugin.on_command(queued)
        await asyncio.wait_for(noticed.wait(), 2)
        await self.plugin.manager.cancel(("bot-a", "10001"))
        await self.drain()
        self.assertEqual(self.browser.ask.await_count, 1)
        queued.bot.call_action.assert_awaited_once()

    async def test_send_budget_is_independent_and_cleanup_still_holds_slot(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def action(name, **kwargs):
            if name.endswith("forward_msg"):
                await asyncio.Event().wait()
            return {"message_id": 6}

        async def cleanup(task_id):
            entered.set()
            await release.wait()
            return m.CleanupResult(m.CleanupStatus.DELETED, task_id)

        self.browser.cleanup.side_effect = cleanup
        await self.ready()
        event = Event(client=SimpleNamespace(call_action=AsyncMock(side_effect=action)))
        with patch.object(m, "SEND_TIMEOUT_SECONDS", 0.02):
            await self.plugin.on_command(event)
            await asyncio.wait_for(entered.wait(), 2)
            self.assertEqual(self.plugin.manager.snapshot().running, 1)
            release.set()
            await self.drain()
        self.assertEqual(
            (await self.plugin.manager.wait(self.browser.ask.call_args.args[0])).result,
            m.JobResult.DELIVERY_UNKNOWN,
        )
        self.assertEqual(
            sum(
                call.args[0].endswith("forward_msg")
                for call in event.bot.call_action.call_args_list
            ),
            1,
        )


if __name__ == "__main__":
    unittest.main()
