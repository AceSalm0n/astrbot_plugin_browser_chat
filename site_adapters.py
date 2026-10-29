"""Site-specific visible controls for browser chat."""

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

COMPOSER = (
    ':is(#prompt-textarea[contenteditable="true"][role="textbox"], '
    '[contenteditable="true"][role="textbox"][data-composer-markdown]):visible'
)
ACCOUNT = ':is([data-testid="accounts-profile-button"], button[aria-label="Open profile menu"]):visible'
NEW_CHAT = ':is(a[aria-label="New chat"], button:has-text("New chat")):visible'


def site_data_root(data_dir: Path, provider: str) -> Path:
    """Resolve the site's isolated data root under the plugin data directory.

    Args:
        data_dir: Dedicated plugin data directory.
        provider: Supported site identifier.

    Returns:
        The original root for ChatGPT or a separate DeepSeek root.

    Raises:
        ValueError: The site is unknown or its path escapes the data directory.
    """
    root = Path(data_dir).resolve()
    if provider == "chatgpt":
        return root
    if provider != "deepseek":
        raise ValueError("Unsupported site")
    target = (root / "sites" / "deepseek").resolve()
    if target == root or not target.is_relative_to(root):
        raise ValueError("Site data path escapes plugin data directory")
    return target


@dataclass(frozen=True)
class TurnSnapshot:
    submission_confirmed: bool
    final_text: str
    generating: bool
    complete: bool


class ChatGPTSite:
    """Read the ChatGPT page without controlling account settings."""

    def __init__(self, origin, error_type):
        self._origin = origin
        self._error = error_type

    async def check_page(self, page, *, structure=False):
        """Inspect visible page health using candidate UI selectors only.

        Args:
            page: An owned page or the dedicated manual account page.
            structure: Also require the new-chat input controls.

        Raises:
            BrowserError: Authentication, challenge, or visible UI is unavailable.
        """
        authentication_page = urlsplit(page.url).hostname == "auth.openai.com"
        if not authentication_page and not page.url.startswith(self._origin + "/"):
            raise self._error("browser")
        account_visible = await page.locator(ACCOUNT).first.is_visible()
        if authentication_page:
            raise self._error("login")
        if "just a moment" in (await page.title()).lower():
            raise self._error("challenge")
        if not account_visible:
            candidates = page.get_by_text("Verify you are human", exact=False)
            for index in range(await candidates.count()):
                candidate = candidates.nth(index)
                if (
                    await candidate.is_visible()
                    and not await candidate.locator(
                        "xpath=ancestor-or-self::*[@data-message-author-role]"
                    ).count()
                ):
                    raise self._error("challenge")
        if await page.get_by_role(
            "button", name="Log in", exact=True
        ).first.is_visible():
            raise self._error("login")
        alerts = page.locator('[role="alert"]:visible')
        for index in range(await alerts.count()):
            alert = (await alerts.nth(index).inner_text()).lower()
            if "too many requests" in alert or "rate limit" in alert:
                raise self._error("rate_limit")
            if "something went wrong" in alert:
                raise self._error("browser")
            if alert.strip() in (
                "services not available in your country",
                "unsupported country, region, or territory",
                "you are connecting from an unsupported country, region, or territory.",
            ):
                raise self._error("unsupported_region")
        if not account_visible:
            # A missing candidate is not evidence of logout: hydration or an
            # unknown layout must remain a structure failure.
            raise self._error("structure")
        if structure:
            # The current site renders Send only after the input is nonempty.
            for selector in (COMPOSER, NEW_CHAT):
                if not await page.locator(selector).first.is_visible():
                    raise self._error("structure")


class DeepSeekSite:
    """Inspect DeepSeek controls without changing the administrator's modes."""

    def __init__(self, origin, error_type):
        self._origin = origin
        self._error = error_type

    def conversation_url(self, conversation_id: str) -> str:
        """Build a canonical URL only for an observed DeepSeek UUID."""
        if not isinstance(conversation_id, str) or not re.fullmatch(
            r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}",
            conversation_id,
        ):
            raise self._error("structure")
        return f"{self._origin}/a/chat/s/{conversation_id}"

    def parse_conversation_url(self, url: str) -> str | None:
        """Accept only this site's root or an exact canonical session address."""
        parsed = urlsplit(url)
        if f"{parsed.scheme}://{parsed.netloc}" != self._origin:
            raise self._error("browser")
        if parsed.query or parsed.fragment:
            raise self._error("structure")
        if parsed.path in ("", "/"):
            return None
        prefix = "/a/chat/s/"
        if not parsed.path.startswith(prefix):
            raise self._error("structure")
        conversation_id = parsed.path[len(prefix) :]
        if self.conversation_url(conversation_id) != url:
            raise self._error("structure")
        return conversation_id

    async def check_page(self, page, *, structure=False):
        """Fail closed on unrecognized origins, login forms, and missing input."""
        if not page.url.startswith(self._origin + "/"):
            raise self._error("browser")
        if await page.locator(
            ".ds-auth-form-wrapper:visible, .ds-sign-in-form-wrapper:visible"
        ).count():
            raise self._error("login")
        if "just a moment" in (await page.title()).lower():
            raise self._error("challenge")
        if structure and await page.locator("textarea:visible").count() != 1:
            raise self._error("structure")

    async def prepare_prompt(self, page, prompt):
        """Fill the prompt while leaving submission and mode toggles untouched."""
        await self.check_page(page, structure=True)
        user_baseline = await page.locator(".ds-message.d29f3d7d:visible").count()
        answer_baseline = await page.locator(
            ".ds-assistant-message-main-content:visible"
        ).count()
        await page.locator("textarea:visible").first.fill(prompt)
        return user_baseline, answer_baseline

    async def submit_prompt(self, page):
        """Submit once; the caller records intent before invoking this method."""
        await page.locator("textarea:visible").first.press("Enter")

    async def prepare_delete(self, page, conversation_url):
        """Open the owned conversation's confirmation without deleting it."""
        conversation_id = self.parse_conversation_url(conversation_url)
        if conversation_id is None or page.url != conversation_url:
            raise self._error("structure")
        entry = page.locator(f'a[href="/a/chat/s/{conversation_id}"]:visible')
        if await entry.count() != 1:
            raise self._error("structure")
        options = entry.locator("[role=button]")
        if await options.count() != 1:
            raise self._error("structure")
        await entry.hover()
        await options.click()
        delete = page.locator(".ds-dropdown-menu-option--error:visible").get_by_text(
            "删除", exact=True
        )
        if await delete.count() != 1:
            raise self._error("structure")
        await delete.click()
        modal = page.locator(".ds-modal:visible")
        confirmation = modal.get_by_role("button", name="删除该对话", exact=True)
        if (
            page.url != conversation_url
            or await entry.count() != 1
            or await modal.count() != 1
            or await confirmation.count() != 1
        ):
            raise self._error("structure")

    async def confirm_delete(self, page, conversation_url):
        """Click once after the owner persisted deletion intent."""
        conversation_id = self.parse_conversation_url(conversation_url)
        if conversation_id is None or page.url != conversation_url:
            raise self._error("structure")
        entry = page.locator(f'a[href="/a/chat/s/{conversation_id}"]:visible')
        modal = page.locator(".ds-modal:visible")
        confirmation = modal.get_by_role("button", name="删除该对话", exact=True)
        if (
            await entry.count() != 1
            or await modal.count() != 1
            or await confirmation.count() != 1
        ):
            raise self._error("structure")
        await confirmation.click()

    async def deletion_confirmed(self, page, conversation_url):
        """Check exact-entry disappearance and an authenticated empty home."""
        conversation_id = self.parse_conversation_url(conversation_url)
        if conversation_id is None or page.url != self._origin + "/":
            return False
        if await page.locator(".ds-modal:visible").count():
            return False
        if await page.locator(f'a[href="/a/chat/s/{conversation_id}"]').count():
            return False
        await self.check_page(page, structure=True)
        return await page.locator(".ds-message:visible").count() == 0

    async def inspect_turn(self, page, prompt, user_baseline, answer_baseline):
        """Read the submitted user turn and only the visible final answer.

        Args:
            page: The current task's owned page.
            prompt: The exact prompt submitted once by the caller.
            user_baseline: Number of visible user messages before submission.
            answer_baseline: Number of final-answer nodes before submission.

        Returns:
            A snapshot; completion still requires a stable window in the caller.

        Raises:
            BrowserError: A different user turn appears or controls are ambiguous.
        """
        await self.check_page(page, structure=True)
        users = page.locator(".ds-message.d29f3d7d:visible")
        submitted = await users.count() > user_baseline
        if submitted:
            text = await users.nth(user_baseline).inner_text()
            if (
                text.replace("\r\n", "\n").strip()
                != prompt.replace("\r\n", "\n").strip()
            ):
                raise self._error("submission_unknown", submission_unknown=True)
        answer = page.locator(".ds-assistant-message-main-content:visible")
        text = ""
        has_body = False
        complete_controls = False
        if submitted and await answer.count() > answer_baseline:
            current = answer.nth(answer_baseline)
            text = (await current.inner_text()).strip()
            has_body = bool(text)
            links = current.locator("a:visible")
            for index in range(await links.count()):
                href = await links.nth(index).get_attribute("href")
                if href:
                    href = urljoin(page.url, href)
                    parsed = urlsplit(href)
                    if (
                        parsed.scheme in ("http", "https")
                        and parsed.netloc
                        and href not in text
                    ):
                        text += "\n" + href
            turn = current.locator("xpath=../..")
            complete_controls = await turn.locator("[role=button]:visible").count() > 0
        field = page.locator("textarea:visible").first
        send = field.locator("xpath=../..").locator(
            ".ds-button--primary.ds-button--circle[role=button]:visible"
        )
        if await send.count() != 1:
            raise self._error("structure")
        button_class = await send.get_attribute("class") or ""
        generating = "ds-button--disabled" not in button_class.split()
        complete = (
            submitted
            and has_body
            and not generating
            and complete_controls
            and not (await field.input_value()).strip()
        )
        return TurnSnapshot(submitted, text, generating, complete)
