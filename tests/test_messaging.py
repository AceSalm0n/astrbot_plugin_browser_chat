"""Pure messaging behavior without AstrBot or browser dependencies."""

import unittest
from enum import Enum
from types import SimpleNamespace

from messaging import PromptError, check_access, extract_prompt, split_answer


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


if __name__ == "__main__":
    unittest.main()
