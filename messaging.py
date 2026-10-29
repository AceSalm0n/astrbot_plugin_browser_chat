"""Text admission, bounded OneBot delivery, and body-free failure notices."""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from string import Formatter
from types import SimpleNamespace
from typing import TYPE_CHECKING

from astrbot.api import logger

if TYPE_CHECKING:
    from .scheduler import SourceRoute

_TRUNCATION_MARKER = "\n\n[内容已截断]"
_CQ_MARKUP = re.compile(r"\[CQ:[^\]\r\n]*\]")


class PromptError(ValueError):
    """A rejected input with a stable, body-free reason code."""

    def __init__(
        self,
        code: str,
        *,
        actual_chars: int | None = None,
        limit_chars: int | None = None,
    ):
        super().__init__(code)
        self.code = code
        self.actual_chars = actual_chars
        self.limit_chars = limit_chars


def _valid_qq_id(value: object) -> bool:
    """Return whether a value is a positive ASCII decimal QQ ID string."""
    return (
        isinstance(value, str)
        and value.isascii()
        and value.isdecimal()
        and any(char != "0" for char in value)
    )


def check_access(sender_id: str, group_id: str | None, config: Mapping) -> bool:
    """Apply independent user and group restrictions, failing closed.

    Args:
        sender_id: A positive ASCII decimal QQ ID string, without whitespace.
        group_id: A QQ group ID string; None or an empty string denotes private chat.
        config: Mapping with boolean restrict_users/restrict_groups and list or
            tuple allowed_users/allowed_groups. user_allowlist_scope is all
            (legacy default) or private. Missing restrictions default to public.
            All supplied restriction fields are validated even when disabled.

    Returns:
        Whether access is permitted. Invalid config or IDs return False. Private
        chats ignore group membership. Group chats check the user allowlist only
        in all scope, and always apply an enabled group allowlist.
    """
    if not isinstance(config, Mapping) or not _valid_qq_id(sender_id):
        return False
    if group_id not in (None, "") and not _valid_qq_id(group_id):
        return False
    scope = config.get("user_allowlist_scope", "all")
    if not isinstance(scope, str) or scope not in ("all", "private"):
        return False
    for kind in ("users", "groups"):
        restricted = config.get(f"restrict_{kind}", False)
        allowed = config.get(f"allowed_{kind}", [])
        if type(restricted) is not bool or not isinstance(allowed, (list, tuple)):
            return False
        if not all(_valid_qq_id(item) for item in allowed):
            return False
    return (
        not config.get("restrict_users", False)
        or (scope == "private" and group_id not in (None, ""))
        or sender_id in config.get("allowed_users", [])
    ) and (
        group_id in (None, "")
        or not config.get("restrict_groups", False)
        or group_id in config.get("allowed_groups", [])
    )


def extract_prompt(event: object, max_input_chars: int = 12000) -> str:
    """Extract current Plain text and direct Reply.chain Plain text only.

    Args:
        event: Duck-typed AstrBot event with public get_messages() and boolean
            is_at_or_wake_command. Component type values match Plain and Reply.
            Current Plain text must begin with exact /ask ai, or ask ai when
            already awakened. A custom wake prefix must be removed by the caller.
        max_input_chars: Positive integer limit on the final combined prompt,
            measured in Python Unicode characters, including combination labels.

    Returns:
        Trimmed outer text with internal whitespace retained. Direct reference
        text precedes the current question with labels when both are present.
        CQ markup is removed; non-Plain components and nested replies are ignored.

    Raises:
        ValueError: If max_input_chars is not a positive integer (bool excluded).
        PromptError: With code invalid_event, invalid_command, empty_prompt,
            reference_unresolved, reference_no_text, or input_too_long. A missing
            chain is unresolved; an empty chain needs sender_id/time metadata to
            establish successful resolution. No error includes message contents.
            Unusable references fail even when a current question exists.
            input_too_long also provides actual_chars/limit_chars without text.
    """
    if type(max_input_chars) is not int or max_input_chars <= 0:
        raise ValueError("max_input_chars must be a positive integer")
    try:
        messages = event.get_messages()
    except Exception as exc:
        raise PromptError("invalid_event") from exc
    if not isinstance(messages, (list, tuple)):
        raise PromptError("invalid_event")

    current = []
    references = []
    for component in messages:
        if getattr(component, "type", None) == "Plain":
            text = getattr(component, "text", None)
            if not isinstance(text, str):
                raise PromptError("invalid_event")
            current.append(text)
        elif getattr(component, "type", None) == "Reply":
            references.append(component)

    current_text = "".join(current).strip()
    command = "/ask ai"
    if getattr(
        event, "is_at_or_wake_command", False
    ) is True and not current_text.startswith("/"):
        command = "ask ai"
    if not current_text.startswith(command) or (
        len(current_text) > len(command) and not current_text[len(command)].isspace()
    ):
        raise PromptError("invalid_command")
    question = _CQ_MARKUP.sub("", current_text[len(command) :]).strip()

    quoted_parts = []
    for reference in references:
        chain = getattr(reference, "chain", None)
        if not isinstance(chain, (list, tuple)):
            raise PromptError("reference_unresolved")
        if not chain:
            sender = getattr(reference, "sender_id", None)
            timestamp = getattr(reference, "time", None)
            resolved = (
                _valid_qq_id(sender)
                or (type(sender) is int and sender > 0)
                or (type(timestamp) in (int, float) and timestamp > 0)
            )
            if not resolved:
                raise PromptError("reference_unresolved")
        quoted = []
        for component in chain:
            if getattr(component, "type", None) == "Plain":
                text = getattr(component, "text", None)
                if not isinstance(text, str):
                    raise PromptError("invalid_event")
                quoted.append(text)
        quoted_text = _CQ_MARKUP.sub("", "".join(quoted)).strip()
        if not quoted_text:
            raise PromptError("reference_no_text")
        quoted_parts.append(quoted_text)

    quoted_text = "\n\n".join(quoted_parts)
    if quoted_text and question:
        prompt = f"引用消息：\n{quoted_text}\n\n本次问题：\n{question}"
    else:
        prompt = quoted_text or question
    if not prompt:
        raise PromptError("empty_prompt")
    if len(prompt) > max_input_chars:
        raise PromptError(
            "input_too_long", actual_chars=len(prompt), limit_chars=max_input_chars
        )
    return prompt


def split_answer(
    text: str, total_limit: int = 12000, node_limit: int = 1500
) -> list[str]:
    """Truncate within the total budget, then split without losing characters.

    Args:
        text: Answer text, including any code or non-BMP characters.
        total_limit: Integer budget including the truncation marker; must be at
            least the marker length. Lengths use Python Unicode characters.
        node_limit: Positive integer node budget, no larger than total_limit.

    Returns:
        Nonempty nodes preserving the truncated text exactly when concatenated.
        Paragraph breaks take priority over line breaks, then hard cuts. Empty
        text returns an empty list. Code fences are preserved without insertion.

    Raises:
        ValueError: If text or either limit is invalid. Bool is not an integer.
    """
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    if type(total_limit) is not int or total_limit < len(_TRUNCATION_MARKER):
        raise ValueError("total_limit must fit the truncation marker")
    if type(node_limit) is not int or not 0 < node_limit <= total_limit:
        raise ValueError("node_limit must be positive and no larger than total_limit")
    if len(text) > total_limit:
        text = text[: total_limit - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER

    parts = []
    offset = 0
    while offset < len(text):
        end = min(offset + node_limit, len(text))
        if end < len(text):
            paragraph = text.rfind("\n\n", offset, end)
            newline = text.rfind("\n", offset, end)
            if paragraph >= offset:
                end = paragraph + 2
            elif newline >= offset:
                end = newline + 1
        parts.append(text[offset:end])
        offset = end
    return parts


SEND_TIMEOUT_SECONDS = 30
NOTIFICATION_WINDOW_SECONDS = 300
DEFAULT_USER_TEMPLATE = "任务 {task_id} 失败：{reason}。"
DEFAULT_ADMIN_TEMPLATE = (
    "[BrowserChat] {category}\n任务：{task_id}\n来源：{source}\n"
    "时间：{time}\n原因：{reason}\n次数：{count}"
)
_LOGGER = logger
_FIELDS = {"task_id", "reason", "category", "source", "time", "count"}
_REASONS = {
    "dependency": "缺少浏览器依赖，请用宿主解释器安装 requirements.txt",
    "login": "需要重新登录网页账号",
    "challenge": "网页需要人工验证",
    "rate_limit": "网页暂时限流",
    "unsupported_region": "网页服务在当前地区不可用",
    "config": "配置需要检查",
    "browser": "浏览器操作失败",
    "structure": "网页结构发生变化",
    "timeout": "操作超时",
    "queue_timeout": "排队超时",
    "timed_out": "操作超时",
    "submission_unknown": "问题提交结果未确认，请勿重复提问",
    "delivery_unknown": "消息发送结果未确认，请勿重复发送",
    "delivery": "消息发送失败",
    "rejected": "平台明确拒绝发送",
    "partial": "消息仅部分发送成功",
    "cleanup": "网页会话清理未完成",
    "cleanup_failed": "网页会话清理失败",
    "cleanup_unknown": "网页会话清理结果未确认",
    "cancelled": "任务已取消",
    "paused": "服务已暂停",
    "closed": "服务已关闭",
    "queue_full": "等待队列已满",
    "user_busy": "你已有未完成任务",
    "access_denied": "没有使用权限",
    "empty_prompt": "问题不能为空",
    "input_too_long": "问题超过长度限制",
    "reference_unresolved": "引用消息读取失败",
    "reference_no_text": "引用消息没有可用文字",
}


class DeliveryStatus(str, Enum):
    CONFIRMED = "confirmed"
    PARTIAL = "partial"
    UNKNOWN = "unknown"
    FAILED = "failed"


@dataclass(frozen=True)
class DeliveryResult:
    status: DeliveryStatus
    confirmed_parts: int = 0
    total_parts: int = 0
    used_fallback: bool = False
    uncertain: bool = False
    reason: str = ""


class DeliveryCancelled(Exception):
    """Carry progress across Python 3.10 task boundaries without losing metadata.

    Callers must record result and then propagate asyncio.CancelledError. A
    CancelledError subclass loses its custom fields at Python 3.10 task joins.
    """

    def __init__(self, result: DeliveryResult):
        super().__init__("delivery_cancelled")
        self.result = result


class OneBotRejected(Exception):
    """An adapter or test double explicitly confirms non-acceptance."""


@dataclass(frozen=True)
class OneBotBinding:
    """Bind one source platform and bot account to its own OneBot client.

    Args:
        platform_id: AstrBot's instance ID, not its platform type name.
        self_id: The originating bot's positive decimal QQ ID.
        client: Raw CQHttp client with async call_action; not event or Context.
        sender_name: Name used for this bot's synthetic forward nodes.
    """

    platform_id: str
    self_id: str
    client: object = field(repr=False, compare=False)
    sender_name: str = "BrowserChat"

    def __post_init__(self):
        if (
            not isinstance(self.platform_id, str)
            or not self.platform_id.strip()
            or not _valid_qq_id(self.self_id)
            or not callable(getattr(self.client, "call_action", None))
            or not isinstance(self.sender_name, str)
            or not 0 < len(self.sender_name) <= 50
            or any(ord(char) < 32 for char in self.sender_name)
        ):
            raise ValueError("Invalid OneBot binding")

    @classmethod
    def from_event(cls, event: object) -> OneBotBinding:
        """Capture public source identifiers and the raw client, not the event.

        Args:
            event: An installed aiocqhttp event exposing the public getters.

        Returns:
            An account-specific binding suitable for SourceRoute.send_handle.

        Raises:
            ValueError: The event is not an identifiable OneBot source.
        """
        try:
            if event.get_platform_name() != "aiocqhttp":
                raise ValueError
            return cls(event.get_platform_id(), event.get_self_id(), event.bot)
        except (AttributeError, TypeError, ValueError):
            raise ValueError("Invalid OneBot event") from None


async def _onebot_action(binding, action, payload, timeout_s):
    """Normalize raw OneBot results; transport uncertainty is never rejection.

    Args:
        binding: Validated source client and bot account.
        action: OneBot action name.
        payload: Text-only OneBot parameters.
        timeout_s: Remaining total delivery budget.

    Returns:
        A delivery status and fixed, body-free reason code.
    """
    try:
        from aiocqhttp.exceptions import ActionFailed, ApiNotAvailable

        rejections = (OneBotRejected, ActionFailed, ApiNotAvailable)
    except ImportError:
        rejections = (OneBotRejected,)
    try:
        response = await asyncio.wait_for(
            binding.client.call_action(action, self_id=binding.self_id, **payload),
            timeout_s,
        )
    except rejections:
        return DeliveryStatus.FAILED, "rejected"
    except asyncio.TimeoutError:
        return DeliveryStatus.UNKNOWN, "timeout"
    except Exception:
        return DeliveryStatus.UNKNOWN, "transport_unknown"
    # CQHttp normally strips the envelope and returns data. None from the raw
    # client is ambiguous; event.send's successful None is a different contract.
    if isinstance(response, dict):
        if response.get("status") == "failed":
            return DeliveryStatus.FAILED, "rejected"
        if "status" in response:
            if response.get("status") == "ok" and response.get("retcode") == 0:
                return DeliveryStatus.CONFIRMED, "accepted"
        elif isinstance(response.get("message_id"), (str, int)) and not isinstance(
            response["message_id"], bool
        ):
            if str(response["message_id"]).strip():
                return DeliveryStatus.CONFIRMED, "accepted"
    return DeliveryStatus.UNKNOWN, "response_unknown"


async def _deliver(route, parts, timeout_s, *, forward):
    """Send once, optionally falling back after an explicit forward rejection.

    Args:
        route: SourceRoute containing a matching OneBotBinding.
        parts: Snapshot of nonempty text nodes.
        timeout_s: One total budget for forward and all fallback nodes.
        forward: Whether to first attempt a merged forward.

    Returns:
        Counts of confirmed nodes and uncertainty, without message bodies.

    Raises:
        ValueError: Text or budget is invalid.
        DeliveryCancelled: Cancellation with the delivery progress attached.
    """
    if (
        not isinstance(parts, (list, tuple))
        or not parts
        or not all(isinstance(part, str) and part for part in parts)
        or isinstance(timeout_s, bool)
        or not isinstance(timeout_s, (int, float))
        or not math.isfinite(timeout_s)
        or timeout_s <= 0
    ):
        raise ValueError("Invalid delivery input")
    parts = tuple(parts)
    binding = getattr(route, "send_handle", None)
    kind = getattr(route, "message_type", None)
    if (
        not isinstance(binding, OneBotBinding)
        or getattr(route, "platform_id", None) != binding.platform_id
        or kind not in ("group", "private")
        or not _valid_qq_id(getattr(route, "target_id", None))
    ):
        return DeliveryResult(
            DeliveryStatus.FAILED, total_parts=len(parts), reason="route"
        )
    target = {"group_id" if kind == "group" else "user_id": int(route.target_id)}
    deadline = asyncio.get_running_loop().time() + timeout_s
    confirmed = 0
    fallback = False
    in_flight = False
    try:
        await asyncio.sleep(0)
        if forward:
            nodes = [
                {
                    "type": "node",
                    "data": {
                        "name": binding.sender_name,
                        "uin": binding.self_id,
                        "content": [{"type": "text", "data": {"text": part}}],
                    },
                }
                for part in parts
            ]
            in_flight = True
            status, reason = await _onebot_action(
                binding,
                f"send_{kind}_forward_msg",
                {**target, "messages": nodes},
                max(0, deadline - asyncio.get_running_loop().time()),
            )
            in_flight = False
            if status != DeliveryStatus.FAILED:
                return DeliveryResult(
                    status,
                    len(parts) if status == DeliveryStatus.CONFIRMED else 0,
                    len(parts),
                    uncertain=status == DeliveryStatus.UNKNOWN,
                    reason=reason,
                )
            fallback = True
        for part in parts:
            await asyncio.sleep(0)
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                status, reason = DeliveryStatus.FAILED, "budget_exhausted"
            else:
                in_flight = True
                status, reason = await _onebot_action(
                    binding,
                    f"send_{kind}_msg",
                    {**target, "message": [{"type": "text", "data": {"text": part}}]},
                    remaining,
                )
                in_flight = False
            if status != DeliveryStatus.CONFIRMED:
                return DeliveryResult(
                    DeliveryStatus.PARTIAL if confirmed else status,
                    confirmed,
                    len(parts),
                    fallback,
                    status == DeliveryStatus.UNKNOWN,
                    reason,
                )
            confirmed += 1
        return DeliveryResult(DeliveryStatus.CONFIRMED, confirmed, len(parts), fallback)
    except asyncio.CancelledError:
        raise DeliveryCancelled(
            DeliveryResult(
                DeliveryStatus.PARTIAL
                if confirmed
                else (DeliveryStatus.UNKNOWN if in_flight else DeliveryStatus.FAILED),
                confirmed,
                len(parts),
                fallback,
                in_flight,
                "cancelled",
            )
        ) from None


async def send_answer(
    route: SourceRoute,
    parts: list[str] | tuple[str, ...],
    *,
    timeout_s: float = SEND_TIMEOUT_SECONDS,
) -> DeliveryResult:
    """Deliver pre-split text using the source bot; confirmation is API acceptance.

    Args:
        route: SourceRoute with OneBotBinding in send_handle.
        parts: Already truncated and split text; this function never regenerates it.
        timeout_s: Shared budget for one forward and at most one plain pass.

    Returns:
        Delivery metadata. Partial/unknown results must never trigger a full retry.

    Raises:
        DeliveryCancelled: Cancellation with progress; callers must keep it.
        ValueError: Invalid text or timeout.
    """
    return await _deliver(route, parts, timeout_s, forward=True)


class TemplateError(ValueError):
    """Invalid configuration; never include the supplied template in diagnostics."""


def validate_template(template: str) -> str:
    """Validate literal text and six simple named placeholders only.

    Args:
        template: Nonempty template, at most 2000 characters.

    Returns:
        The validated original string.

    Raises:
        TemplateError: Unmatched braces, lookup, conversion, or formatting syntax.
    """
    try:
        if (
            not isinstance(template, str)
            or not template.strip()
            or len(template) > 2000
        ):
            raise ValueError
        for _, name, spec, conversion in Formatter().parse(template):
            if name is not None and (name not in _FIELDS or spec or conversion):
                raise ValueError
    except ValueError:
        raise TemplateError("invalid_template") from None
    return template


@dataclass(frozen=True)
class Fault:
    """Failure metadata only; reason must be a code, never an exception/body.

    Args:
        task_id: Task identifier, or a fixed startup identifier.
        source: Original SourceRoute, or None for a startup fault.
        category: Controlled failure category.
        reason: Controlled reason code. Unknown text is discarded at rendering.
        browser_account_scope: Stable non-secret browser profile identifier.
        occurred_at: Aware UTC occurrence time, retained in merged summaries.
    """

    task_id: str
    source: SourceRoute | None
    category: str
    reason: str = ""
    browser_account_scope: str = "default"
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if not all(
            isinstance(item, str)
            for item in (
                self.task_id,
                self.category,
                self.reason,
                self.browser_account_scope,
            )
        ):
            raise ValueError("Invalid fault metadata")
        if (
            not isinstance(self.occurred_at, datetime)
            or self.occurred_at.tzinfo is None
        ):
            raise ValueError("Invalid fault time")


def _safe_identifier(value):
    """Allow bounded metadata IDs, excluding paths, credentials and control text."""
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", value)
        or re.search(r"cookie|token|authorization|bearer|^sk-", value, re.IGNORECASE)
    ):
        return "[已隐藏]"
    return value


def render_failure(
    fault: Fault, *, template: str | None = None, admin: bool = False, count: int = 1
) -> str:
    """Render safe metadata; raw errors and arbitrary reason strings are omitted.

    Args:
        fault: Body-free failure metadata.
        template: User-configured text; invalid values log and use the safe default.
        admin: Select the administrator default rather than the user default.
        count: Number of occurrences in this notification window.

    Returns:
        A bounded notice. Substituted metadata cannot add lines or CQ segments.
    """
    default = DEFAULT_ADMIN_TEMPLATE if admin else DEFAULT_USER_TEMPLATE
    try:
        template = validate_template(default if template is None else template)
    except TemplateError:
        _LOGGER.warning("Invalid failure template; using default")
        template = default
    source = fault.source
    source_text = "startup"
    if source is not None:
        source_text = "{}/{}/{}".format(
            _safe_identifier(getattr(source, "platform_id", None)),
            _safe_identifier(getattr(source, "message_type", None)),
            _safe_identifier(getattr(source, "target_id", None)),
        )
    category = fault.category if fault.category in _REASONS else "unknown"
    values = {
        "task_id": _safe_identifier(fault.task_id),
        "category": category,
        "reason": _REASONS.get(
            fault.reason, _REASONS.get(category, "操作失败，详细信息已隐藏")
        ),
        "source": source_text,
        "time": fault.occurred_at.astimezone(timezone.utc).isoformat(
            timespec="seconds"
        ),
        "count": str(count if type(count) is int and count > 0 else 1),
    }
    return template.format_map(values)[:4000]


async def send_failure(
    route: SourceRoute,
    fault: Fault,
    *,
    template: str | None = None,
    timeout_s: float = SEND_TIMEOUT_SECONDS,
) -> DeliveryResult:
    """Send one plain user notice without initiating administrator notifications.

    Args:
        route: Exact source route with a bound OneBot client.
        fault: Body-free fault metadata.
        template: User failure template; administrator templates are independent.
        timeout_s: Total notice budget.

    Returns:
        Delivery metadata; caller decides whether an administrator fault is needed.
    """
    return await _deliver(
        route, [render_failure(fault, template=template)], timeout_s, forward=False
    )


class FailureNotifier:
    """Own platform-specific administrator notices and 300-second merge timers.

    Args:
        bindings: Snapshot of platform IDs to their exact OneBot bindings.
        admin_ids: Snapshot of administrator QQ IDs; every source uses its own bot.
        admin_failure_template: Independent administrator message template.
        window_sleep: Awaitable delay for the merge window; injectable for tests.
    """

    def __init__(
        self,
        bindings: Mapping[str, OneBotBinding],
        admin_ids,
        *,
        admin_failure_template: str = DEFAULT_ADMIN_TEMPLATE,
        window_sleep=asyncio.sleep,
    ):
        if (
            not isinstance(bindings, Mapping)
            or any(
                not isinstance(item, OneBotBinding) or key != item.platform_id
                for key, item in bindings.items()
            )
            or not isinstance(admin_ids, (list, tuple))
            or not all(_valid_qq_id(item) for item in admin_ids)
        ):
            raise ValueError("Invalid notification configuration")
        self._bindings = dict(bindings)
        self._admins = tuple(dict.fromkeys(admin_ids))
        try:
            self._template = validate_template(admin_failure_template)
        except TemplateError:
            _LOGGER.warning("Invalid administrator template; using default")
            self._template = DEFAULT_ADMIN_TEMPLATE
        if not self._admins:
            _LOGGER.warning("Administrator notifications are not configured")
        self._windows = {}
        self._tasks = set()
        self._closed = False
        self._window_sleep = window_sleep

    async def notify_failure(self, platform_id: str, fault: Fault) -> None:
        """Send the first occurrence immediately; later occurrences share a summary.

        Args:
            platform_id: Originating AstrBot instance, never a guessed fallback.
            fault: Failure metadata; None source is allowed for startup broadcasts
                explicitly enumerated by the future caller.

        Returns:
            None. Failure is logged without exception text or recursive notices.
        """
        if self._closed or not self._admins:
            return
        if platform_id not in self._bindings or (
            fault.source is not None and fault.source.platform_id != platform_id
        ):
            _LOGGER.warning(
                "Administrator notification skipped: route mismatch platform=%s",
                _safe_identifier(platform_id),
            )
            return
        category = fault.category if fault.category in _REASONS else "unknown"
        key = (platform_id, _safe_identifier(fault.browser_account_scope), category)
        if key in self._windows:
            self._windows[key]["count"] += 1
            return
        window = {"fault": fault, "count": 1}
        self._windows[key] = window
        timer = asyncio.create_task(self._expire(key, window))
        self._tasks.add(timer)
        timer.add_done_callback(self._tasks.discard)
        sending = asyncio.create_task(self._send_admins(platform_id, fault, 1))
        self._tasks.add(sending)
        sending.add_done_callback(self._tasks.discard)
        await sending

    async def _expire(self, key, window):
        await self._window_sleep(NOTIFICATION_WINDOW_SECONDS)
        if self._windows.get(key) is window:
            del self._windows[key]
            if window["count"] > 1:
                await self._send_admins(key[0], window["fault"], window["count"])

    async def _send_admins(self, platform_id, fault, count):
        binding = self._bindings[platform_id]
        text = render_failure(fault, template=self._template, admin=True, count=count)
        for admin_id in self._admins:
            route = SimpleNamespace(
                platform_id=platform_id,
                message_type="private",
                target_id=admin_id,
                send_handle=binding,
            )
            try:
                result = await _deliver(
                    route, [text], SEND_TIMEOUT_SECONDS, forward=False
                )
            except DeliveryCancelled as error:
                _LOGGER.warning(
                    "Administrator notification cancelled: platform=%s admin=%s uncertain=%s",
                    _safe_identifier(platform_id),
                    admin_id,
                    error.result.uncertain,
                )
                raise asyncio.CancelledError from None
            if result.status != DeliveryStatus.CONFIRMED:
                _LOGGER.warning(
                    "Administrator notification failed: platform=%s admin=%s status=%s reason=%s",
                    _safe_identifier(platform_id),
                    admin_id,
                    result.status.value,
                    result.reason,
                )

    async def close(self) -> None:
        """Cancel and join owned timers/sends; do not send summaries during unload."""
        self._closed = True
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._windows.clear()
