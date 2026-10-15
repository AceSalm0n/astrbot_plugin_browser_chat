"""Pure messaging behavior without AstrBot or browser dependencies."""

import asyncio
import unittest
from datetime import datetime, timezone
from enum import Enum
from types import SimpleNamespace
from unittest.mock import AsyncMock

from messaging import (
    DeliveryCancelled,
    DeliveryStatus,
    FailureNotifier,
    Fault,
    OneBotBinding,
    OneBotRejected,
    PromptError,
    TemplateError,
    check_access,
    extract_prompt,
    render_failure,
    send_answer,
    send_failure,
    split_answer,
    validate_template,
)
from scheduler import SourceRoute


class ComponentType(str, Enum):
    Plain = "Plain"
    Reply = "Reply"


def plain(text):
    return SimpleNamespace(type=ComponentType.Plain, text=text)


def reply(chain=None, **metadata):
    return SimpleNamespace(
        type=ComponentType.Reply,
        chain=chain,
        sender_id=metadata.get("sender_id", 0),
        time=metadata.get("time", 0),
        message_str=metadata.get("message_str", ""),
        text=metadata.get("text", ""),
    )


def event(*chain, awake=False, message_str=""):
    return SimpleNamespace(
        get_messages=lambda: list(chain),
        is_at_or_wake_command=awake,
        message_str=message_str,
    )


class AccessTests(unittest.TestCase):
    def test_four_restriction_combinations(self):
        for groups, users, sender, group, expected in (
            (False, False, "99", "99", True),
            (True, False, "99", "20", True),
            (True, False, "10", "99", False),
            (False, True, "10", "99", True),
            (False, True, "99", "20", False),
            (True, True, "10", "20", True),
            (True, True, "99", "20", False),
            (True, True, "10", "99", False),
        ):
            with self.subTest(groups=groups, users=users, sender=sender, group=group):
                self.assertIs(
                    check_access(
                        sender,
                        group,
                        {
                            "restrict_groups": groups,
                            "allowed_groups": ["20"],
                            "restrict_users": users,
                            "allowed_users": ["10"],
                        },
                    ),
                    expected,
                )

    def test_defaults_are_public(self):
        self.assertTrue(check_access("10", "20", {}))

    def test_private_chat_ignores_group_restriction(self):
        for group in (None, ""):
            self.assertTrue(check_access("10", group, {"restrict_groups": True}))
            self.assertFalse(check_access("10", group, {"restrict_users": True}))
            self.assertTrue(
                check_access(
                    "10",
                    group,
                    {
                        "restrict_groups": True,
                        "restrict_users": True,
                        "allowed_users": ["10"],
                    },
                )
            )

    def test_enabled_empty_lists_deny(self):
        self.assertFalse(check_access("10", "20", {"restrict_groups": True}))
        self.assertFalse(check_access("10", "20", {"restrict_users": True}))

    def test_invalid_switches_fail_closed(self):
        for value in ("false", 0, 1, None, [], float("nan"), float("inf")):
            for key in ("restrict_groups", "restrict_users"):
                with self.subTest(value=value, key=key):
                    self.assertFalse(check_access("10", "20", {key: value}))

    def test_invalid_ids_and_lists_fail_closed(self):
        for value in (True, 10, 1.0, " ", " 10", "10 ", "a", "0", "１２"):
            self.assertFalse(check_access(value, "20", {}))
            self.assertFalse(check_access("10", value, {}))
            self.assertFalse(check_access("10", "20", {"allowed_users": [value]}))
        for value in ("10", None, {"10"}, {"10": True}, 1):
            self.assertFalse(check_access("10", "20", {"allowed_users": value}))
        self.assertFalse(check_access("10", "20", None))


class PromptTests(unittest.TestCase):
    def assert_prompt_error(self, code, incoming, **kwargs):
        with self.assertRaises(PromptError) as raised:
            extract_prompt(incoming, **kwargs)
        self.assertEqual(raised.exception.code, code)
        self.assertIsInstance(raised.exception, ValueError)

    def test_literal_command_preserves_internal_newlines(self):
        self.assertEqual(
            extract_prompt(event(plain("/ask ai\nline 1\n\nline 2"))),
            "line 1\n\nline 2",
        )

    def test_command_may_span_plain_components(self):
        self.assertEqual(
            extract_prompt(event(plain("/ask "), plain("ai question"))), "question"
        )

    def test_awakened_command_accepts_at(self):
        self.assertEqual(
            extract_prompt(
                event(SimpleNamespace(type="At"), plain("ask ai question"), awake=True)
            ),
            "question",
        )

    def test_command_requires_exact_tokens_and_wakeup(self):
        for text, awake in (
            ("ask ai question", False),
            ("/ask aibot question", True),
            ("ask aibot question", True),
            ("/ask ai!", False),
            ("/ask  ai q", False),
            ("prefix /ask ai q", True),
            ("#ask ai q", False),
        ):
            with self.subTest(text=text, awake=awake):
                self.assert_prompt_error(
                    "invalid_command", event(plain(text), awake=awake)
                )

    def test_truthy_non_boolean_wakeup_does_not_authorize(self):
        self.assert_prompt_error(
            "invalid_command", event(plain("ask ai q"), awake="true")
        )

    def test_current_and_direct_reference_have_labels(self):
        self.assertEqual(
            extract_prompt(
                event(reply([plain("quoted\ntext")]), plain("/ask ai question"))
            ),
            "引用消息：\nquoted\ntext\n\n本次问题：\nquestion",
        )

    def test_reference_only_is_prompt(self):
        self.assertEqual(
            extract_prompt(event(reply([plain("quoted")]), plain("/ask ai"))), "quoted"
        )

    def test_non_text_and_nested_references_are_not_serialized(self):
        self.assertEqual(
            extract_prompt(
                event(
                    reply(
                        [
                            SimpleNamespace(type="Image", text="image caption"),
                            reply([plain("nested")]),
                            plain("direct"),
                        ]
                    ),
                    plain("/ask ai"),
                    SimpleNamespace(type="Image", text="caption"),
                )
            ),
            "direct",
        )

    def test_cq_markup_is_not_prompt_text(self):
        self.assertEqual(
            extract_prompt(
                event(
                    reply([plain("quote[CQ:image,file=x]")]),
                    plain("/ask ai [CQ:at,qq=10]question"),
                )
            ),
            "引用消息：\nquote\n\n本次问题：\nquestion",
        )

    def test_unresolved_reference_rejects_even_with_current_question(self):
        for chain in (None, []):
            self.assert_prompt_error(
                "reference_unresolved",
                event(
                    reply(chain, text="must not use", message_str="fallback"),
                    plain("/ask ai still a question"),
                ),
            )

    def test_resolved_empty_reference_is_distinct(self):
        self.assert_prompt_error(
            "reference_no_text",
            event(reply([], sender_id="10", time=123), plain("/ask ai question")),
        )

    def test_reference_without_plain_text_is_rejected(self):
        for chain in (
            [SimpleNamespace(type="Image")],
            [reply([plain("nested")])],
            [plain(" \n")],
        ):
            self.assert_prompt_error(
                "reference_no_text", event(reply(chain), plain("/ask ai question"))
            )

    def test_empty_question_is_rejected(self):
        self.assert_prompt_error("empty_prompt", event(plain("/ask ai \n")))

    def test_input_limit_uses_combined_prompt(self):
        self.assertEqual(
            len(extract_prompt(event(plain("/ask ai " + "x" * 12000)))), 12000
        )
        self.assert_prompt_error(
            "input_too_long", event(plain("/ask ai " + "x" * 12001))
        )
        self.assert_prompt_error(
            "input_too_long", event(reply([plain("x" * 11990)]), plain("/ask ai q"))
        )

    def test_overlong_input_reports_lengths_without_retaining_body(self):
        incoming = event(reply([plain("quote")]), plain("/ask ai question"))
        with self.assertRaises(PromptError) as raised:
            extract_prompt(incoming, max_input_chars=10)
        failure = raised.exception
        self.assertEqual(failure.code, "input_too_long")
        self.assertEqual(
            failure.actual_chars, len("引用消息：\nquote\n\n本次问题：\nquestion")
        )
        self.assertEqual(failure.limit_chars, 10)
        self.assertNotIn("question", str(failure))

    def test_invalid_limits_are_rejected(self):
        for value in (True, False, 0, -1, 1.5, "12", float("inf"), float("nan")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                extract_prompt(event(plain("/ask ai q")), value)

    def test_malformed_event_is_explicit(self):
        self.assert_prompt_error("invalid_event", SimpleNamespace())
        self.assert_prompt_error("invalid_event", event(plain(None)))


class AnswerTests(unittest.TestCase):
    def test_empty_answer_has_no_nodes(self):
        self.assertEqual(split_answer(""), [])

    def test_exact_total_limit_is_not_marked(self):
        text = "x" * 12000
        parts = split_answer(text)
        self.assertEqual("".join(parts), text)
        self.assertTrue(all(0 < len(part) <= 1500 for part in parts))

    def test_truncation_marker_is_in_total_budget(self):
        parts = split_answer("x" * 20000)
        self.assertEqual("".join(parts), "x" * 11991 + "\n\n[内容已截断]")
        self.assertEqual(sum(map(len, parts)), 12000)
        self.assertTrue(all(0 < len(part) <= 1500 for part in parts))

    def test_paragraphs_take_priority_over_later_line_break(self):
        self.assertEqual(
            split_answer("aaa\n\nbbb\nccc", 100, 10), ["aaa\n\n", "bbb\nccc"]
        )

    def test_newlines_take_priority_over_hard_cut(self):
        self.assertEqual(
            split_answer("abc\ndefghijkl", 100, 7), ["abc\n", "defghij", "kl"]
        )

    def test_hard_cut_preserves_non_bmp_and_code(self):
        text = "```python\nprint('😀')\n```\n\n" + "😀" * 4000
        parts = split_answer(text)
        self.assertEqual("".join(parts), text)
        self.assertTrue(all(0 < len(part) <= 1500 for part in parts))

    def test_small_node_limit_does_not_lose_marker(self):
        parts = split_answer("x" * 100, 9, 2)
        self.assertEqual("".join(parts), "\n\n[内容已截断]")
        self.assertTrue(all(len(part) <= 2 for part in parts))

    def test_all_whitespace_is_preserved(self):
        self.assertEqual("".join(split_answer("\n\n \n")), "\n\n \n")

    def test_invalid_limits_and_nontext_are_rejected(self):
        for value in (True, False, 0, -1, 1.5, "12", float("inf"), float("nan")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                split_answer("text", value, 1)
            with self.subTest(value=value), self.assertRaises(ValueError):
                split_answer("text", 100, value)
        for total, node in ((8, 1), (10, 11)):
            with self.assertRaises(ValueError):
                split_answer("text", total, node)
        with self.assertRaises(ValueError):
            split_answer(None)


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = SimpleNamespace(
            call_action=AsyncMock(return_value={"message_id": 12})
        )
        self.binding = OneBotBinding("bot-a", "90001", self.client)
        self.route = SourceRoute("bot-a", "group", "12345", self.binding)

    async def test_group_and_private_forward_route_and_nodes(self):
        for kind, target in (("group", "group_id"), ("private", "user_id")):
            with self.subTest(kind=kind):
                route = SourceRoute("bot-a", kind, "12345", self.binding)
                result = await send_answer(route, ["first", "[CQ:at,qq=all]\nsecond"])
                self.assertEqual(result.status, DeliveryStatus.CONFIRMED)
                self.assertEqual(result.confirmed_parts, 2)
                (action,) = self.client.call_action.call_args.args
                params = self.client.call_action.call_args.kwargs
                self.assertEqual(action, f"send_{kind}_forward_msg")
                self.assertEqual(params[target], 12345)
                self.assertEqual(params["self_id"], "90001")
                self.assertEqual(params["messages"][0]["data"]["uin"], "90001")
                self.assertEqual(params["messages"][0]["data"]["name"], "BrowserChat")
                self.assertEqual(
                    params["messages"][1]["data"]["content"],
                    [{"type": "text", "data": {"text": "[CQ:at,qq=all]\nsecond"}}],
                )

    async def test_explicit_rejection_falls_back_only_once(self):
        for rejected in (OneBotRejected(), {"status": "failed", "retcode": 100}):
            with self.subTest(rejected=type(rejected).__name__):
                self.client.call_action.reset_mock()
                self.client.call_action.side_effect = [
                    rejected,
                    {"message_id": 1},
                    {"message_id": 2},
                ]
                result = await send_answer(self.route, ["a", "b"])
                self.assertEqual(result.status, DeliveryStatus.CONFIRMED)
                self.assertTrue(result.used_fallback)
                self.assertEqual(
                    [c.args[0] for c in self.client.call_action.call_args_list],
                    ["send_group_forward_msg", "send_group_msg", "send_group_msg"],
                )

    async def test_installed_sdk_rejection_and_unwrapped_data_semantics(self):
        try:
            from aiocqhttp.api_impl import _handle_api_result
            from aiocqhttp.exceptions import ActionFailed
        except ImportError:
            self.skipTest("Run without -S to verify the installed OneBot SDK")
        accepted = _handle_api_result(
            {"status": "ok", "retcode": 0, "data": {"message_id": 12}}
        )
        self.assertEqual(accepted, {"message_id": 12})
        self.client.call_action.side_effect = [
            ActionFailed({"status": "failed", "retcode": 100}),
            accepted,
        ]
        result = await send_answer(self.route, ["a"])
        self.assertEqual(result.status, DeliveryStatus.CONFIRMED)
        self.assertTrue(result.used_fallback)
        self.assertEqual(self.client.call_action.await_count, 2)

    async def test_unknown_forward_result_never_resends(self):
        for response in (
            None,
            False,
            True,
            {},
            {"status": "async", "retcode": 1},
            asyncio.TimeoutError(),
            ConnectionError("secret response"),
        ):
            with self.subTest(response=type(response).__name__):
                self.client.call_action.reset_mock()
                self.client.call_action.side_effect = [response]
                result = await send_answer(self.route, ["a", "b"])
                self.assertEqual(result.status, DeliveryStatus.UNKNOWN)
                self.assertEqual(result.confirmed_parts, 0)
                self.client.call_action.assert_awaited_once()

    async def test_partial_plain_failure_preserves_confirmed_count(self):
        for failure, uncertain in (
            (OneBotRejected(), False),
            (asyncio.TimeoutError(), True),
        ):
            self.client.call_action.reset_mock()
            self.client.call_action.side_effect = [
                OneBotRejected(),
                {"message_id": 1},
                failure,
            ]
            result = await send_answer(self.route, ["a", "b", "c"])
            self.assertEqual(result.status, DeliveryStatus.PARTIAL)
            self.assertEqual(result.confirmed_parts, 1)
            self.assertEqual(result.uncertain, uncertain)
            self.assertEqual(self.client.call_action.await_count, 3)

    async def test_route_mismatch_or_missing_binding_never_sends(self):
        for route in (
            SourceRoute("bot-b", "group", "12345", self.binding),
            SourceRoute("bot-a", "other", "12345", self.binding),
            SourceRoute("bot-a", "group", "invalid", self.binding),
            SourceRoute("bot-a", "group", "12345"),
        ):
            result = await send_answer(route, ["a"])
            self.assertEqual(result.status, DeliveryStatus.FAILED)
        self.client.call_action.assert_not_called()

    async def test_total_send_budget_and_inflight_cancellation(self):
        entered = asyncio.Event()

        async def blocked(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        self.client.call_action.side_effect = blocked
        result = await send_answer(self.route, ["a"], timeout_s=0.02)
        self.assertEqual(result.status, DeliveryStatus.UNKNOWN)
        task = asyncio.create_task(send_answer(self.route, ["a"]))
        entered.clear()
        await entered.wait()
        task.cancel()
        with self.assertRaises(DeliveryCancelled) as caught:
            await task
        self.assertTrue(caught.exception.result.uncertain)
        self.assertEqual(self.client.call_action.await_count, 2)

    async def test_event_none_wrapper_is_not_used_as_onebot_result(self):
        event = SimpleNamespace(
            get_platform_name=lambda: "aiocqhttp",
            get_platform_id=lambda: "bot-a",
            get_self_id=lambda: "90001",
            bot=self.client,
            send=AsyncMock(return_value=None),
        )
        route = SourceRoute(
            "bot-a", "private", "12345", OneBotBinding.from_event(event)
        )
        self.assertEqual(
            (await send_answer(route, ["a"])).status, DeliveryStatus.CONFIRMED
        )
        event.send.assert_not_called()
        event.get_platform_name = lambda: "webchat"
        with self.assertRaises(ValueError):
            OneBotBinding.from_event(event)

    async def test_user_failure_has_own_template_and_no_raw_reason(self):
        fault = Fault("task-1", self.route, "browser", "Cookie: secret\nBearer token")
        result = await send_failure(
            self.route, fault, template="用户 {task_id}: {reason}"
        )
        self.assertEqual(result.status, DeliveryStatus.CONFIRMED)
        text = self.client.call_action.call_args.kwargs["message"][0]["data"]["text"]
        self.assertTrue(text.startswith("用户 task-1:"))
        self.assertNotIn("secret", text)
        self.assertNotIn("token", text)


class TemplateTests(unittest.TestCase):
    def test_template_validation_blocks_lookup_conversion_and_format_specs(self):
        for template in (
            "{unknown}",
            "{reason.__class__}",
            "{task_id[0]}",
            "{reason!r}",
            "{count:09d}",
            "{reason:{task_id}}",
            "{",
            "",
            None,
        ):
            with self.subTest(template=template), self.assertRaises(TemplateError):
                validate_template(template)
        validate_template(
            "{{literal}} {task_id}\n{reason} {category} {source} {time} {count}"
        )

    def test_bad_template_falls_back_and_substitutions_cannot_inject_lines(self):
        fault = Fault(
            "task\nCookie=secret",
            SourceRoute("bot-a", "group", "12345"),
            "login",
            "Authorization: Bearer secret; C:\\private\\file",
            occurred_at=datetime(2026, 10, 15, tzinfo=timezone.utc),
        )
        with self.assertLogs("messaging", level="WARNING") as logs:
            text = render_failure(fault, template="{reason.__class__}")
        self.assertNotIn("secret", text + str(logs.output))
        self.assertEqual(len(text.splitlines()), 1)
        self.assertIn("登录", text)
        self.assertNotIn("private", render_failure(fault, admin=True))
        self.assertEqual(
            render_failure(fault, template="{time}"), "2026-10-15T00:00:00+00:00"
        )


class NotificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.window_release = asyncio.Event()
        self.window_lengths = []

        async def controlled_window(seconds):
            self.window_lengths.append(seconds)
            await self.window_release.wait()

        self.client = SimpleNamespace(
            call_action=AsyncMock(return_value={"message_id": 1})
        )
        self.binding = OneBotBinding("bot-a", "90001", self.client)
        self.route = SourceRoute("bot-a", "group", "12345", self.binding)
        self.fault = Fault("task-1", self.route, "login", "login")
        self.notifier = FailureNotifier(
            {"bot-a": self.binding},
            ["10001", "10002"],
            admin_failure_template="管理员 {category} {count} {source}",
            window_sleep=controlled_window,
        )

    async def asyncTearDown(self):
        await self.notifier.close()

    async def test_multiple_admins_exact_platform_and_no_recursive_exception(self):
        self.client.call_action.side_effect = [RuntimeError("Cookie secret"), False]
        with self.assertLogs("messaging", level="WARNING") as logs:
            await self.notifier.notify_failure("bot-a", self.fault)
        self.assertEqual(self.client.call_action.await_count, 2)
        self.assertNotIn("secret", str(logs.output))
        self.assertEqual(
            {c.kwargs["user_id"] for c in self.client.call_action.call_args_list},
            {10001, 10002},
        )
        self.assertTrue(
            all(
                c.kwargs["self_id"] == "90001"
                for c in self.client.call_action.call_args_list
            )
        )

    async def test_missing_or_mismatched_route_never_uses_another_platform(self):
        second_client = SimpleNamespace(
            call_action=AsyncMock(return_value={"message_id": 2})
        )
        second = OneBotBinding("bot-b", "90002", second_client)
        await self.notifier.close()
        self.notifier = FailureNotifier(
            {"bot-a": self.binding, "bot-b": second}, ["10001"]
        )
        with self.assertLogs("messaging", level="WARNING"):
            await self.notifier.notify_failure("bot-b", self.fault)
        self.client.call_action.assert_not_called()
        second_client.call_action.assert_not_called()
        await self.notifier.notify_failure(
            "bot-b", Fault("task-2", None, "login", "login")
        )
        self.client.call_action.assert_not_called()
        self.assertEqual(second_client.call_action.call_args.kwargs["self_id"], "90002")

    async def test_cancelling_or_closing_during_admin_send_preserves_cancellation(self):
        for closing in (False, True):
            with self.subTest(closing=closing):
                entered = asyncio.Event()

                async def blocked(*args, **kwargs):
                    entered.set()
                    await asyncio.Event().wait()

                self.client.call_action.reset_mock()
                self.client.call_action.side_effect = blocked
                notifier = FailureNotifier({"bot-a": self.binding}, ["10001", "10002"])
                try:
                    task = asyncio.create_task(
                        notifier.notify_failure("bot-a", self.fault)
                    )
                    await entered.wait()
                    if closing:
                        await notifier.close()
                    else:
                        task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    self.client.call_action.assert_awaited_once()
                finally:
                    await notifier.close()

    async def test_first_immediate_then_one_summary_and_no_singleton_summary(self):
        import messaging

        self.assertEqual(messaging.NOTIFICATION_WINDOW_SECONDS, 300)
        await self.notifier.notify_failure("bot-a", self.fault)
        self.assertEqual(self.client.call_action.await_count, 2)
        await self.notifier.notify_failure("bot-a", self.fault)
        other_source = Fault(
            "task-2", SourceRoute("bot-a", "private", "54321"), "login", "login"
        )
        await self.notifier.notify_failure("bot-a", other_source)
        self.assertEqual(self.client.call_action.await_count, 2)
        self.assertEqual(self.window_lengths, [300])
        self.window_release.set()
        await asyncio.gather(*tuple(self.notifier._tasks))
        self.assertEqual(self.client.call_action.await_count, 4)
        self.assertIn(
            " 3 ",
            self.client.call_action.call_args.kwargs["message"][0]["data"]["text"],
        )
        await self.notifier.notify_failure("bot-a", self.fault)
        await asyncio.gather(*tuple(self.notifier._tasks))
        self.assertEqual(self.client.call_action.await_count, 6)

    async def test_scopes_and_categories_have_independent_windows_and_close_joins(self):
        await self.notifier.notify_failure("bot-a", self.fault)
        await self.notifier.notify_failure(
            "bot-a", Fault("task-2", self.route, "cleanup", "cleanup")
        )
        await self.notifier.notify_failure(
            "bot-a", Fault("task-3", self.route, "login", "login", "second-profile")
        )
        await self.notifier.notify_failure("bot-a", self.fault)
        self.assertEqual(self.client.call_action.await_count, 6)
        await self.notifier.close()
        self.window_release.set()
        self.assertEqual(self.client.call_action.await_count, 6)

    async def test_config_snapshot_empty_admins_and_invalid_template(self):
        admins = ["10001"]
        bindings = {"bot-a": self.binding}
        with self.assertLogs("messaging", level="WARNING"):
            notifier = FailureNotifier(
                bindings, admins, admin_failure_template="{unknown}"
            )
        try:
            admins.clear()
            bindings.clear()
            await notifier.notify_failure("bot-a", self.fault)
            self.assertEqual(self.client.call_action.await_count, 1)
        finally:
            await notifier.close()
        with self.assertLogs("messaging", level="WARNING"):
            empty = FailureNotifier({"bot-a": self.binding}, [])
        await empty.notify_failure("bot-a", self.fault)
        await empty.close()
        self.assertEqual(self.client.call_action.await_count, 1)


if __name__ == "__main__":
    unittest.main()
