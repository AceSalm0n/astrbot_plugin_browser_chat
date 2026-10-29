"""Site-specific visible controls for browser chat."""

from pathlib import Path
from urllib.parse import urlsplit

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
