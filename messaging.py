"""Pure text and admission logic for browser chat."""

import re
from collections.abc import Mapping

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
            tuple allowed_users/allowed_groups. Missing fields default to public.
            All supplied restriction fields are validated even when disabled.

    Returns:
        Whether access is permitted. Invalid config or IDs return False. Private
        chats ignore group membership; group chats must pass both enabled lists.
    """
    if not isinstance(config, Mapping) or not _valid_qq_id(sender_id):
        return False
    if group_id not in (None, "") and not _valid_qq_id(group_id):
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
