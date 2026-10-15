"""Owned browser pages, visible ChatGPT interaction, and bounded cleanup."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlsplit

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Playwright

START_TIMEOUT_SECONDS = 30
CLOSE_TIMEOUT_SECONDS = 20
CLEANUP_TIMEOUT_SECONDS = 20
STABLE_WINDOW_SECONDS = 2
POLL_INTERVAL_SECONDS = 0.1

# Candidate selectors need separate validation against the authenticated site.
COMPOSER = '#prompt-textarea[contenteditable="true"][role="textbox"]'
ACCOUNT = '[data-testid="accounts-profile-button"]'
NEW_CHAT = 'a[aria-label="New chat"]'
SEND = '[data-testid="send-button"]'
STOP = '[data-testid="stop-button"]'
USER = '[data-message-author-role="user"]'
ASSISTANT = '[data-message-author-role="assistant"]'
COPY = '[data-testid="copy-turn-action-button"]'
CONVERSATION_ID = re.compile(r"[A-Za-z0-9_-]{1,200}")


class BrowserStatus(str, Enum):
    STARTING = "starting"
    READY = "ready"
    PAUSED = "paused"
    STOPPING = "stopping"


class CleanupStatus(str, Enum):
    NOT_CREATED = "not_created"
    RETAINED = "retained"
    DELETED = "deleted"
    CLEANUP_FAILED = "cleanup_failed"
    CLEANUP_UNKNOWN = "cleanup_unknown"


@dataclass(frozen=True)
class BrowserAnswer:
    text: str
    conversation_id: str | None


@dataclass(frozen=True)
class CleanupResult:
    status: CleanupStatus
    task_id: str
    conversation_id: str | None = None
    reason: str = ""


class BrowserError(RuntimeError):
    """Expose a fixed diagnostic category without page or request content.

    Args:
        category: A safe machine-readable diagnostic code.
        submission_unknown: Whether the page may have accepted a submission.
    """

    def __init__(self, category: str, *, submission_unknown: bool = False) -> None:
        if not re.fullmatch(r"[a-z_]+", category):
            raise ValueError("Invalid diagnostic category")
        self.category = category
        self.submission_unknown = submission_unknown
        super().__init__(category)


class ChatGPTBrowser:
    """Own one dedicated context and conservatively checked task sessions.

    Args:
        data_dir: Plugin data directory from StarTools.get_data_dir() in the
            future host integration; never a daily browser profile directory.
        channel: Installed browser channel. Only Chrome is currently tested.
        base_url: ChatGPT root URL, or a loopback root for local UI tests.
    """

    def __init__(
        self,
        data_dir: Path,
        channel: str = "chrome",
        *,
        base_url: str = "https://chatgpt.com/",
    ) -> None:
        parsed = urlsplit(base_url)
        try:
            loopback = (
                parsed.hostname == "localhost"
                or ipaddress.ip_address(parsed.hostname or "").is_loopback
            )
        except ValueError:
            loopback = False
        if (
            parsed.scheme not in ("http", "https")
            or (parsed.hostname != "chatgpt.com" and not loopback)
            or (parsed.hostname == "chatgpt.com" and parsed.scheme != "https")
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Unsupported browser origin")
        self.base_url = base_url.rstrip("/") + "/"
        self._origin = f"{parsed.scheme}://{parsed.netloc}"
        self.profile_dir = Path(data_dir).resolve() / "browser_profile"
        self._registry_path = Path(data_dir).resolve() / "owned_sessions.json"
        self.channel = channel
        self.status = BrowserStatus.PAUSED
        self.pause_reason = "config"
        self.context: BrowserContext | None = None
        self._driver: Playwright | None = None
        self._startup_task: asyncio.Task[Playwright | None] | None = None
        self._abandon_start = False
        self._lock = asyncio.Lock()
        self._operations = asyncio.Lock()
        self._closing = False
        self._resuming = False
        self._settings_verified = False
        self._close_task = None
        self._asks = {}
        self._closing_requests = set()
        self._pages = {}
        self._opening = {}
        self._cleanups = {}
        self._late_closers = {}
        self._deleting = set()
        self._probe_key = object()
        self._records = {}
        self._registry_valid = True
        self._records_dirty = False
        self._load_records()
        self._recovery = set(self._records)

    async def start(self) -> None:
        """Open the owned browser, keeping admission paused until site checks.

        Raises:
            RuntimeError: Playwright is missing; installation is always manual.
            Exception: Browser startup failed after releasing owned resources.
        """
        async with self._lock:
            if self._closing:
                raise BrowserError("closed")
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
        if asyncio.current_task() in self._asks.values():
            raise RuntimeError("An ask task cannot close its own browser")
        if self._close_task is None or self._close_task.done():
            self._closing = True
            self.status = BrowserStatus.STOPPING
            self._close_task = asyncio.create_task(self._close_owned())
        await asyncio.shield(self._close_task)

    async def _close_owned(self):
        """Wait for owned work before releasing the dedicated context.

        Raises:
            Exception: The owned context or driver could not be released.
        """
        try:
            requests = list(self._asks.values())
            self._closing_requests = set(requests)
            for request in requests:
                request.cancel()
            await asyncio.gather(*requests, return_exceptions=True)
            async with self._lock:
                for task_id in set(self._pages) | set(self._opening):
                    if task_id is self._probe_key:
                        continue
                    if task_id not in self._cleanups:
                        self._cleanups[task_id] = asyncio.create_task(
                            self._cleanup_owned(task_id)
                        )
                await asyncio.gather(*self._cleanups.values(), return_exceptions=True)
                pending = [
                    item for item in self._late_closers.values() if not item.done()
                ]
                if pending:
                    # Context shutdown below terminates any still-pending opener.
                    await asyncio.wait(pending, timeout=CLOSE_TIMEOUT_SECONDS / 2)
                await self._release()
                for item in (*self._opening.values(), *self._late_closers.values()):
                    if not item.done():
                        item.cancel()
                await asyncio.gather(
                    *self._opening.values(),
                    *self._late_closers.values(),
                    return_exceptions=True,
                )
                self._opening.clear()
                self._pages.clear()
                self._late_closers.clear()
        finally:
            self._closing = False
            self._closing_requests.clear()
            self._settings_verified = False

    def _load_records(self):
        """Reject untrusted ownership files without overwriting their bytes.

        Returns:
            None. Invalid files leave the instance paused for cleanup.
        """
        if not self._registry_path.exists():
            return
        try:
            payload = json.loads(self._registry_path.read_text(encoding="utf-8"))
            if (
                not isinstance(payload, dict)
                or set(payload) != {"version", "sessions"}
                or type(payload["version"]) is not int
                or payload["version"] != 1
                or not isinstance(payload["sessions"], dict)
            ):
                raise ValueError
            fields = {
                "task_id",
                "created_at",
                "conversation_id",
                "url",
                "keep",
                "answer_result",
                "cleanup",
                "submission",
            }
            for task_id, record in payload["sessions"].items():
                if (
                    not isinstance(task_id, str)
                    or not task_id
                    or len(task_id) > 200
                    or not isinstance(record, dict)
                    or set(record) != fields
                    or record["task_id"] != task_id
                    or type(record["keep"]) is not bool
                    or record["submission"]
                    not in ("not_attempted", "attempted", "confirmed")
                    or record["answer_result"]
                    not in ("pending", "success", "failed", "cancelled", "timed_out")
                    or record["cleanup"]
                    not in ("pending", *(status.value for status in CleanupStatus))
                    or datetime.fromisoformat(record["created_at"]).tzinfo is None
                ):
                    raise ValueError
                conversation_id = record["conversation_id"]
                if conversation_id is None:
                    if record["url"] is not None:
                        raise ValueError
                elif (
                    not isinstance(conversation_id, str)
                    or not CONVERSATION_ID.fullmatch(conversation_id)
                    or record["submission"] != "confirmed"
                    or record["url"] != f"{self._origin}/c/{conversation_id}"
                ):
                    raise ValueError
            self._records = payload["sessions"]
        except (OSError, ValueError, TypeError, KeyError):
            self._registry_valid = False
            self._pause("cleanup")

    def _save_records(self):
        """Atomically persist ownership metadata, never question or answer text.

        Raises:
            BrowserError: Ownership cannot be durably recorded.
        """
        if not self._registry_valid:
            raise BrowserError("cleanup")
        self._records_dirty = True
        temporary = None
        try:
            self._registry_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self._registry_path.parent,
                prefix=".owned_sessions-",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump({"version": 1, "sessions": self._records}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._registry_path)
            self._records_dirty = False
        except OSError:
            self._pause("cleanup")
            raise BrowserError("cleanup") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _pause(self, reason):
        self.status = BrowserStatus.PAUSED
        self.pause_reason = {
            "structure": "browser",
            "unsupported_region": "config",
        }.get(reason, reason)
        if reason in ("login", "challenge"):
            self._settings_verified = False

    async def _check_page(self, page, *, structure=False):
        """Inspect visible page health using candidate UI selectors only.

        Args:
            page: An owned page or the dedicated manual account page.
            structure: Also require the new-chat input controls.

        Raises:
            BrowserError: Authentication, challenge, or visible UI is unavailable.
        """
        authentication_page = urlsplit(page.url).hostname == "auth.openai.com"
        if not authentication_page and not page.url.startswith(self._origin + "/"):
            raise BrowserError("browser")
        account_visible = await page.locator(ACCOUNT).first.is_visible()
        if authentication_page:
            raise BrowserError("login")
        if "just a moment" in (await page.title()).lower():
            raise BrowserError("challenge")
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
                    raise BrowserError("challenge")
        if await page.get_by_role(
            "button", name="Log in", exact=True
        ).first.is_visible():
            raise BrowserError("login")
        alerts = page.locator('[role="alert"]:visible')
        for index in range(await alerts.count()):
            alert = (await alerts.nth(index).inner_text()).lower()
            if "too many requests" in alert or "rate limit" in alert:
                raise BrowserError("rate_limit")
            if "something went wrong" in alert:
                raise BrowserError("browser")
            if alert.strip() in (
                "services not available in your country",
                "unsupported country, region, or territory",
                "you are connecting from an unsupported country, region, or territory.",
            ):
                raise BrowserError("unsupported_region")
        if not account_visible:
            raise BrowserError("login")
        if structure:
            for selector in (COMPOSER, SEND, NEW_CHAT):
                if not await page.locator(selector).first.is_visible():
                    raise BrowserError("structure")

    async def resume(self, *, account_settings_verified: bool = False) -> BrowserStatus:
        """Check residual ownership and visible login before opening admission.

        Args:
            account_settings_verified: Human confirmation that memory and custom
                instructions were checked for this authenticated account.

        Returns:
            READY only after every ownership and readiness check succeeds.
        """
        if type(account_settings_verified) is not bool:
            raise ValueError("Account confirmation must be boolean")
        async with self._operations:
            if (
                self._closing
                or self._resuming
                or self._asks
                or self._pages
                or self._opening
                or any(not item.done() for item in self._cleanups.values())
            ):
                raise BrowserError("busy")
            self._resuming = True
        try:
            async with self._lock:
                if self.context is None:
                    self._pause("browser")
                    return self.status
                if not self._registry_valid:
                    self._pause("cleanup")
                    return self.status
                if self._records_dirty:
                    try:
                        self._save_records()
                    except BrowserError:
                        return self.status
                for task_id in tuple(self._recovery):
                    record = self._records[task_id]
                    if record["cleanup"] in ("deleted", "retained", "not_created"):
                        self._recovery.discard(task_id)
                        continue
                    result = await self._cleanup_owned(task_id)
                    if result.status in (
                        CleanupStatus.CLEANUP_FAILED,
                        CleanupStatus.CLEANUP_UNKNOWN,
                    ):
                        return self.status
                    self._recovery.discard(task_id)
                self._settings_verified |= account_settings_verified
                if not self._settings_verified:
                    self._pause("config")
                    return self.status
                # Reserve a portion of the single probe budget for page closure.
                deadline = asyncio.get_running_loop().time() + CLEANUP_TIMEOUT_SECONDS
                available = False
                try:
                    await asyncio.wait_for(
                        self._probe_ready(), CLEANUP_TIMEOUT_SECONDS * 0.9
                    )
                    available = True
                except asyncio.CancelledError:
                    raise
                except BrowserError as error:
                    self._pause(
                        "browser" if error.category == "structure" else error.category
                    )
                except Exception:
                    self._pause("browser")
                finally:
                    disposed = await self._dispose_page(self._probe_key, deadline)
                    if not disposed:
                        self._pause("cleanup")
                if not available or not disposed or self._closing:
                    return self.status
                self.status = BrowserStatus.READY
                self.pause_reason = None
                return self.status
        finally:
            self._resuming = False

    async def _probe_ready(self):
        """Inspect a disposable owned page inside resume's total probe budget.

        Raises:
            BrowserError: The visible login or required controls are unavailable.
        """
        self._opening[self._probe_key] = asyncio.create_task(self.context.new_page())
        probe = await asyncio.shield(self._opening[self._probe_key])
        self._pages[self._probe_key] = probe
        await probe.goto(self.base_url, wait_until="domcontentloaded")
        await self._check_page(probe, structure=True)

    async def ask(
        self, task_id: str, prompt: str, timeout_s: float, *, keep: bool = False
    ) -> BrowserAnswer:
        """Submit once and return only this request's completed visible answer.

        Args:
            task_id: Unique task ownership key.
            prompt: The single user message, held only in memory.
            timeout_s: Budget for the complete page and generation flow.
            keep: Immutable retention policy captured before page submission.

        Returns:
            The completed visible answer and its observed conversation ID.

        Raises:
            BrowserError: Page failure, timeout, or uncertain submission.
            asyncio.CancelledError: The caller cancelled the operation.
            ValueError: Invalid request arguments.
        """
        if (
            not isinstance(task_id, str)
            or not task_id
            or len(task_id) > 200
            or not isinstance(prompt, str)
            or not prompt.strip()
            or type(keep) is not bool
            or isinstance(timeout_s, bool)
            or not isinstance(timeout_s, (int, float))
            or not math.isfinite(timeout_s)
            or timeout_s <= 0
        ):
            raise ValueError("Invalid browser request")
        deadline = asyncio.get_running_loop().time() + timeout_s
        async with self._operations:
            if (
                self._closing
                or self._resuming
                or self.context is None
                or self.status != BrowserStatus.READY
                or not self._registry_valid
            ):
                raise BrowserError("not_ready")
            if task_id in self._records:
                raise BrowserError("duplicate_task")
            record = {
                "task_id": task_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "conversation_id": None,
                "url": None,
                "keep": keep,
                "answer_result": "pending",
                "cleanup": "pending",
                "submission": "not_attempted",
            }
            self._records[task_id] = record
            self._recovery.add(task_id)
            self._save_records()
            self._asks[task_id] = asyncio.current_task()
        try:
            remaining = deadline - asyncio.get_running_loop().time()
            answer = await asyncio.wait_for(self._ask_page(task_id, prompt), remaining)
            record["answer_result"] = "success"
            self._save_records()
            return answer
        except asyncio.CancelledError:
            record["answer_result"] = "cancelled"
            try:
                self._save_records()
            except BrowserError:
                pass
            raise
        except Exception as error:
            timed_out = isinstance(error, asyncio.TimeoutError)
            record["answer_result"] = "timed_out" if timed_out else "failed"
            category = error.category if isinstance(error, BrowserError) else "browser"
            if category in ("login", "challenge"):
                self._pause(category)
            if timed_out:
                category = "timeout"
            if record["submission"] == "attempted":
                category = "submission_unknown"
            if category in ("login", "challenge", "rate_limit", "unsupported_region"):
                self._pause(category)
            elif self.pause_reason not in ("cleanup", "login", "challenge"):
                self._pause("browser")
            try:
                self._save_records()
            except BrowserError:
                category = (
                    "cleanup"
                    if record["submission"] != "attempted"
                    else "submission_unknown"
                )
            raise BrowserError(
                category, submission_unknown=record["submission"] == "attempted"
            ) from None
        finally:
            self._asks.pop(task_id, None)

    async def _ask_page(self, task_id, prompt):
        """Run the linear UI flow within ask's single timeout budget.

        Args:
            task_id: Registered owner of the newly created page.
            prompt: User input to submit exactly once.

        Returns:
            A BrowserAnswer after the current assistant turn is complete.

        Raises:
            BrowserError: A visible failure or unsupported structure occurs.
        """
        record = self._records[task_id]
        self._opening[task_id] = asyncio.create_task(self.context.new_page())
        page = await asyncio.shield(self._opening[task_id])
        self._pages[task_id] = page
        if self._closing:
            raise BrowserError("closed")
        await page.goto(self.base_url, wait_until="domcontentloaded")
        await self._check_page(page, structure=True)
        await page.locator(NEW_CHAT).first.click()
        await self._check_page(page, structure=True)
        if urlsplit(page.url).path not in ("", "/"):
            raise BrowserError("structure")
        users = await page.locator(USER).count()
        assistants = await page.locator(ASSISTANT).count()
        await page.locator(COMPOSER).fill(prompt)
        record["submission"] = "attempted"
        self._save_records()
        await page.locator(SEND).click()
        stable_text = None
        stable_since = None
        while True:
            if (
                record["submission"] != "confirmed"
                and await page.locator(USER).count() > users
            ):
                submitted = page.locator(USER).nth(users)
                visible_text = submitted.locator(".markdown:visible")
                if await visible_text.count():
                    submitted_text = "\n".join(
                        [
                            await visible_text.nth(index).inner_text()
                            for index in range(await visible_text.count())
                        ]
                    )
                else:
                    submitted_text = await submitted.inner_text()
                if (
                    submitted_text.replace("\r\n", "\n").strip()
                    != prompt.replace("\r\n", "\n").strip()
                ):
                    raise BrowserError("submission_unknown", submission_unknown=True)
                record["submission"] = "confirmed"
                self._save_records()
            if record["submission"] == "confirmed":
                # URL changes alone cannot establish that this task owns a chat.
                self._capture_conversation(record, page.url)
            await self._check_page(page)
            if (
                record["submission"] == "confirmed"
                and await page.locator(ASSISTANT).count() > assistants
            ):
                answer = page.locator(ASSISTANT).nth(assistants)
                markdown = answer.locator(".markdown:visible")
                if not await answer.locator(".markdown").count():
                    if not await page.locator(STOP).first.is_visible():
                        raise BrowserError("structure")
                elif not await markdown.count():
                    stable_text, stable_since = None, None
                else:
                    parts = []
                    for index in range(await markdown.count()):
                        body = markdown.nth(index)
                        parts.append(await body.inner_text())
                        links = body.locator("a:visible")
                        for link_index in range(await links.count()):
                            href = await links.nth(link_index).get_attribute("href")
                            if href:
                                href = urljoin(page.url, href)
                            if (
                                href
                                and href.startswith(("https://", "http://"))
                                and href not in parts[-1]
                            ):
                                parts.append(href)
                    text = "\n".join(parts).strip()
                    stopped = not await page.locator(STOP).first.is_visible()
                    complete = await answer.locator(COPY).first.is_visible()
                    if text and stopped and not complete:
                        raise BrowserError("structure")
                    if text and stopped and complete:
                        now = asyncio.get_running_loop().time()
                        if stable_text != text:
                            stable_text, stable_since = text, now
                        elif now - stable_since >= STABLE_WINDOW_SECONDS:
                            return BrowserAnswer(text, record["conversation_id"])
                    else:
                        stable_text, stable_since = None, None
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    def _capture_conversation(self, record, url):
        parsed = urlsplit(url)
        if parsed.hostname == "auth.openai.com":
            raise BrowserError("login")
        if f"{parsed.scheme}://{parsed.netloc}" != self._origin:
            raise BrowserError("browser")
        if not parsed.path.startswith("/c/"):
            return
        conversation_id = parsed.path[3:]
        if (
            not CONVERSATION_ID.fullmatch(conversation_id)
            or parsed.query
            or parsed.fragment
            or record["conversation_id"] not in (None, conversation_id)
        ):
            raise BrowserError("structure")
        if record["conversation_id"] is None:
            record["conversation_id"], record["url"] = conversation_id, url
            self._save_records()

    async def cleanup(self, task_id: str, keep: bool | None = None) -> CleanupResult:
        """Share bounded cleanup while respecting the recorded retention policy.

        Args:
            task_id: Exact registered ownership key.
            keep: Compatibility argument; cannot change the recorded policy.

        Returns:
            An independent cleanup result without changing the answer outcome.
        """
        if keep is not None and type(keep) is not bool:
            raise ValueError("Retention must be boolean")
        closing = None
        async with self._operations:
            if self._resuming:
                raise BrowserError("busy")
            if task_id in self._asks:
                raise BrowserError("busy")
            if task_id not in self._records:
                if not self._registry_valid:
                    self._pause("cleanup")
                    return CleanupResult(
                        CleanupStatus.CLEANUP_UNKNOWN,
                        task_id,
                        reason="invalid_registry",
                    )
                return CleanupResult(
                    CleanupStatus.NOT_CREATED, task_id, reason="not_registered"
                )
            if (
                self._closing
                and task_id not in self._cleanups
                and asyncio.current_task() not in self._closing_requests
            ):
                # A cancelled ask's own finally must finish before close can
                # finish waiting for that caller; unrelated late work waits.
                closing = self._close_task
            elif task_id not in self._cleanups:
                self._cleanups[task_id] = asyncio.create_task(
                    self._cleanup_owned(task_id)
                )
            task = self._cleanups.get(task_id)
        if closing is not None:
            await asyncio.shield(closing)
            return await self.cleanup(task_id, keep=keep)
        return await asyncio.shield(task)

    async def _cleanup_owned(self, task_id):
        """Bound deletion and close this task's own page even on failure.

        Args:
            task_id: Exact task record to retain or delete.

        Returns:
            A safe cleanup status, including uncertain deletion outcomes.
        """
        record = self._records.get(task_id)
        status = CleanupStatus.NOT_CREATED
        reason = "not_registered"
        deadline = asyncio.get_running_loop().time() + CLEANUP_TIMEOUT_SECONDS
        try:
            status, reason = await asyncio.wait_for(
                self._cleanup_page(task_id, record), CLEANUP_TIMEOUT_SECONDS * 0.9
            )
        except asyncio.TimeoutError:
            status, reason = CleanupStatus.CLEANUP_UNKNOWN, "timeout"
        except Exception as error:
            if isinstance(error, BrowserError) and error.category in (
                "login",
                "challenge",
            ):
                self._settings_verified = False
            status = (
                CleanupStatus.CLEANUP_UNKNOWN
                if task_id in self._deleting
                else CleanupStatus.CLEANUP_FAILED
            )
            reason = "confirmation_unknown" if task_id in self._deleting else "browser"
        finally:
            disposed = await self._dispose_page(task_id, deadline)
            if disposed is None:
                status, reason = CleanupStatus.CLEANUP_UNKNOWN, "page_close_unknown"
            elif not disposed:
                status, reason = CleanupStatus.CLEANUP_FAILED, "page_close"
            self._deleting.discard(task_id)
        if status in (CleanupStatus.CLEANUP_FAILED, CleanupStatus.CLEANUP_UNKNOWN):
            self._pause("cleanup")
        if record is not None and self._registry_valid:
            record["cleanup"] = status.value
            try:
                self._save_records()
            except BrowserError:
                status, reason = CleanupStatus.CLEANUP_UNKNOWN, "record_write"
        if status in (
            CleanupStatus.NOT_CREATED,
            CleanupStatus.RETAINED,
            CleanupStatus.DELETED,
        ):
            self._recovery.discard(task_id)
        result = CleanupResult(
            status, task_id, record["conversation_id"] if record else None, reason
        )
        return result

    async def _dispose_page(self, task_id, deadline):
        """Keep a shielded late closer registered until ownership is released.

        Args:
            task_id: Task or private probe page owner.
            deadline: Monotonic deadline shared with the surrounding operation.

        Returns:
            True for a closed page, False for a known failure, or None if a
            tracked closer is still pending after the caller's budget.
        """
        if task_id not in self._pages and task_id not in self._opening:
            return True
        closer = self._late_closers.get(task_id)
        if closer is None or closer.done():
            closer = asyncio.create_task(self._close_task_page(task_id))
            self._late_closers[task_id] = closer
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return None
        try:
            return await asyncio.wait_for(asyncio.shield(closer), remaining)
        except asyncio.TimeoutError:
            return None

    async def _close_task_page(self, task_id):
        """Finish an owned opener and close its page without losing references.

        Args:
            task_id: Registered owner of a page or pending new-page operation.

        Returns:
            Whether no page remains owned by this key.
        """
        page = self._pages.get(task_id)
        opening = self._opening.get(task_id)
        if page is None and opening is not None:
            try:
                page = await asyncio.shield(opening)
                self._pages[task_id] = page
            except Exception:
                self._opening.pop(task_id, None)
                return True
        try:
            if page is not None and not page.is_closed():
                await asyncio.wait_for(page.close(), CLOSE_TIMEOUT_SECONDS / 2)
        except Exception:
            return False
        self._pages.pop(task_id, None)
        self._opening.pop(task_id, None)
        return True

    async def _cleanup_page(self, task_id, record):
        """Delete only an exact owned URL and require visible confirmation.

        Args:
            task_id: Owner of any existing task page.
            record: Validated immutable ownership policy and observed identity.

        Returns:
            A cleanup status and a fixed diagnostic code.
        """
        if not self._registry_valid:
            return CleanupStatus.CLEANUP_UNKNOWN, "invalid_registry"
        if record is None:
            return CleanupStatus.NOT_CREATED, "not_registered"
        if record["cleanup"] in ("retained", "deleted", "not_created"):
            return CleanupStatus(record["cleanup"]), "already_cleaned"
        if record["keep"]:
            return CleanupStatus.RETAINED, "retained"
        if record["conversation_id"] is None:
            if record["submission"] == "not_attempted":
                return CleanupStatus.NOT_CREATED, "not_submitted"
            return CleanupStatus.CLEANUP_UNKNOWN, "conversation_unknown"
        if self.context is None:
            return CleanupStatus.CLEANUP_FAILED, "browser_closed"
        page = self._pages.get(task_id)
        if page is None or page.is_closed():
            opening = self._opening.get(task_id)
            if opening is None:
                opening = asyncio.create_task(self.context.new_page())
                self._opening[task_id] = opening
            page = await asyncio.shield(opening)
            self._pages[task_id] = page
            if page.is_closed():
                return CleanupStatus.CLEANUP_FAILED, "page_closed"
        if page.url != record["url"]:
            await page.goto(record["url"], wait_until="domcontentloaded")
        if page.url != record["url"]:
            return CleanupStatus.CLEANUP_UNKNOWN, "conversation_mismatch"
        await self._check_page(page)
        await page.get_by_role(
            "button", name="Conversation options", exact=True
        ).click()
        await page.get_by_role("menuitem", name="Delete", exact=True).click()
        dialog = page.get_by_role("dialog", name="Delete chat", exact=True)
        if page.url != record["url"]:
            return CleanupStatus.CLEANUP_UNKNOWN, "conversation_mismatch"
        self._deleting.add(task_id)
        await dialog.get_by_role("button", name="Delete", exact=True).click()
        while True:
            alerts = page.locator('[role="alert"]:visible')
            for index in range(await alerts.count()):
                if "deletion failed" in (await alerts.nth(index).inner_text()).lower():
                    return CleanupStatus.CLEANUP_FAILED, "deletion_rejected"
            statuses = page.locator('[role="status"]:visible')
            confirmed = False
            for index in range(await statuses.count()):
                if (await statuses.nth(index).inner_text()).strip() == "Chat deleted":
                    confirmed = True
            if confirmed and page.url != record["url"]:
                if not await page.locator(
                    f'a[href="/c/{record["conversation_id"]}"]:visible'
                ).count():
                    return CleanupStatus.DELETED, "deleted"
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

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
