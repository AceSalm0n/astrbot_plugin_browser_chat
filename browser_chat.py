"""Owned browser pages, visible ChatGPT interaction, and bounded cleanup."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import os
import re
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlsplit

from astrbot.api import logger

try:
    from .site_adapters import (
        COMPOSER,
        NEW_CHAT,
        ChatGPTSite,
        DeepSeekSite,
        site_data_root,
    )
except ImportError:
    from site_adapters import (
        COMPOSER,
        NEW_CHAT,
        ChatGPTSite,
        DeepSeekSite,
        site_data_root,
    )

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Playwright

START_TIMEOUT_SECONDS = 30
CLOSE_TIMEOUT_SECONDS = 20
CLEANUP_TIMEOUT_SECONDS = 20
STABLE_WINDOW_SECONDS = 2
POLL_INTERVAL_SECONDS = 0.1
PAGE_READY_TIMEOUT_SECONDS = 12
WINDOW_CONTROL_SECONDS = 0.2
WINDOW_RESERVE_SECONDS = 1

# Current semantic controls were observed on the authenticated site; retain
# previous selectors for the local fixtures and older site layouts.
SEND = (
    ':is([data-testid="send-button"], button[type="submit"][aria-label="Send"]):visible'
)
STOP = ':is([data-testid="stop-button"], button[aria-label="Stop"]):visible'
USER = (
    ':is([data-message-author-role="user"], [data-user-message-bubble="true"]):visible'
)
ASSISTANT = ':is([data-message-author-role="assistant"], [data-content-search-unit-key$=":assistant"]):visible'
COPY = ':is([data-testid="copy-turn-action-button"], button[aria-label="Copy"]):visible'
ANSWER_BODY = ':is(.markdown, [data-markdown-text-style="assistant-message"])'
CONVERSATION_ID = re.compile(r"[A-Za-z0-9_-]{1,200}")
CLIENT_CONVERSATION_ID = re.compile(
    r"local-chatgpt:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
PROFILE_NAME = re.compile(r"[A-Za-z0-9_-]{0,64}")
RESERVED_PROFILE_NAMES = {"CON", "PRN", "AUX", "NUL"} | {
    f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)
}


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


@dataclass(frozen=True)
class CleanupIssue:
    task_id: str
    phase: str
    reason: str


@dataclass(frozen=True)
class CleanupCheckResult:
    task_id: str
    state: str


class BrowserError(RuntimeError):
    """Expose a fixed diagnostic category without page or request content.

    Args:
        category: A safe machine-readable diagnostic code.
        submission_unknown: Whether the page may have accepted a submission.
        site_category: Underlying visible site fault, when separately known.
    """

    def __init__(
        self,
        category: str,
        *,
        submission_unknown: bool = False,
        site_category: str | None = None,
    ) -> None:
        if not re.fullmatch(r"[a-z_]+", category):
            raise ValueError("Invalid diagnostic category")
        if site_category not in (
            None,
            "login",
            "challenge",
            "rate_limit",
            "unsupported_region",
        ):
            raise ValueError("Invalid site fault category")
        self.category = category
        self.submission_unknown = submission_unknown
        self.site_category = site_category
        super().__init__(category)


class ChatGPTBrowser:
    """Own one dedicated context and conservatively checked task sessions.

    Args:
        data_dir: Plugin data directory from StarTools.get_data_dir() in the
            future host integration; never a daily browser profile directory.
        channel: Installed Chrome or Edge browser channel.
        base_url: ChatGPT root URL, or a loopback root for local UI tests.
        profile_name: Dedicated directory name; empty selects the channel default.
        start_minimized: Best-effort Windows window policy; defaults to unchanged UI.
    """

    def __init__(
        self,
        data_dir: Path,
        channel: str = "chrome",
        *,
        base_url: str = "https://chatgpt.com/",
        profile_name: str = "",
        start_minimized: bool = False,
        provider: str = "chatgpt",
    ) -> None:
        if type(start_minimized) is not bool:
            raise ValueError("Invalid browser window policy")
        if channel not in ("chrome", "msedge"):
            raise ValueError("Invalid browser channel")
        if (
            not isinstance(profile_name, str)
            or PROFILE_NAME.fullmatch(profile_name) is None
            or profile_name.upper() in RESERVED_PROFILE_NAMES
        ):
            raise ValueError("Invalid browser profile name")
        profile_name = profile_name or (
            "browser_profile" if channel == "chrome" else "browser_profile_msedge"
        )
        data_root = site_data_root(data_dir, provider)
        profile_dir = data_root / profile_name
        registry_path = data_root / (
            "owned_sessions.json"
            if profile_dir == data_root / "browser_profile"
            else f"owned_sessions.{profile_name}.json"
        )
        for path in (profile_dir, registry_path):
            resolved = path.resolve()
            if resolved == data_root or not resolved.is_relative_to(data_root):
                raise ValueError("Browser profile path escapes plugin data directory")
        parsed = urlsplit(base_url)
        try:
            loopback = (
                parsed.hostname == "localhost"
                or ipaddress.ip_address(parsed.hostname or "").is_loopback
            )
        except ValueError:
            loopback = False
        expected_host = "chatgpt.com" if provider == "chatgpt" else "chat.deepseek.com"
        if (
            parsed.scheme not in ("http", "https")
            or (parsed.hostname != expected_host and not loopback)
            or (parsed.hostname == expected_host and parsed.scheme != "https")
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Unsupported browser origin")
        self.base_url = base_url.rstrip("/") + "/"
        self._origin = f"{parsed.scheme}://{parsed.netloc}"
        self._site = (ChatGPTSite if provider == "chatgpt" else DeepSeekSite)(
            self._origin, BrowserError
        )
        self.provider = provider
        self.profile_dir = profile_dir
        self._registry_path = registry_path
        self.channel = channel
        self.start_minimized = start_minimized
        self._window_lock = asyncio.Lock()
        self._window_generation = 0
        self._window_jobs = set()
        self._window_warned = False
        self.status = BrowserStatus.PAUSED
        self.pause_reason = "config"
        self.context: BrowserContext | None = None
        self._driver: Playwright | None = None
        self._startup_task: asyncio.Task[Playwright | None] | None = None
        self._abandon_start = False
        self._lock = asyncio.Lock()
        self._operations = asyncio.Lock()
        self._cleanup_lock = asyncio.Lock()
        self._maintenance_task = None
        self._home_page = None
        self._home_key = object()
        self._check_key = object()
        self._closing = False
        self._resuming = False
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
        self._migration_source = None
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
                if self.start_minimized and os.name == "nt" and self.context.pages:
                    self._window_generation += 1
                    await self._window_call(
                        self.context.pages[0],
                        WINDOW_CONTROL_SECONDS,
                        self._window_generation,
                        minimize=True,
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
        if (
            asyncio.current_task() in self._asks.values()
            or asyncio.current_task() is self._maintenance_task
        ):
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
            self._window_generation += 1
            window_jobs = tuple(self._window_jobs)
            for job in window_jobs:
                job.cancel()
            if window_jobs:
                await asyncio.wait(window_jobs, timeout=WINDOW_CONTROL_SECONDS)
            maintenance = self._maintenance_task
            if maintenance is not None and not maintenance.done():
                maintenance.cancel()
                await asyncio.gather(maintenance, return_exceptions=True)
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
            for job in tuple(self._window_jobs):
                job.cancel()
            await asyncio.gather(*tuple(self._window_jobs), return_exceptions=True)
            self._closing = False
            self._closing_requests.clear()

    def _window_warning(self):
        """Report optional UI degradation once without changing business status."""
        if not self._window_warned:
            self._window_warned = True
            logger.warning("Optional browser window control unavailable")

    def _window_done(self, task):
        """Release a tracked control job and observe any late exception."""
        self._window_jobs.discard(task)
        if not task.cancelled():
            task.exception()

    async def _window_rpc(self, page, token, minimize, expected_id):
        """Inspect or minimize exactly one owned target's browser window.

        Args:
            page: Page owned by this dedicated context.
            token: Generation invalidated by overlapping opens or cancellation.
            minimize: Whether to attempt the optional state change.
            expected_id: Snapshot window ID, or None for initial startup.

        Returns:
            Window ID/state snapshot, or None when control is unavailable.
        """
        session = None
        attach = asyncio.create_task(self.context.new_cdp_session(page))
        self._window_jobs.add(attach)
        attach.add_done_callback(self._window_done)
        try:
            session = await asyncio.shield(attach)
            result = await session.send("Browser.getWindowForTarget")
            window_id = result["windowId"]
            state = result["bounds"]["windowState"]
            if type(window_id) is not int or state not in (
                "normal",
                "minimized",
                "maximized",
                "fullscreen",
            ):
                raise ValueError("Invalid window state")
            if (
                minimize
                and (expected_id is None or expected_id == window_id)
                and token == self._window_generation
                and not self._window_warned
                and not self._closing
                and state != "minimized"
            ):
                await session.send(
                    "Browser.setWindowBounds",
                    {"windowId": window_id, "bounds": {"windowState": "minimized"}},
                )
            return window_id, state
        except Exception:
            self._window_warning()
            return None
        finally:
            if session is None and not attach.cancelled():
                try:
                    session = await asyncio.shield(attach)
                except Exception:
                    pass
            if session is not None:
                try:
                    await session.detach()
                except Exception:
                    self._window_warning()

    async def _window_call(
        self, page, budget, token, *, minimize=False, expected_id=None
    ):
        """Bound the whole control job, including attach, reads and detach.

        Args:
            page: Already registered owned page.
            budget: Remaining shared display allowance in seconds.
            token: Current page-opening generation.
            minimize: Whether a state change is allowed.
            expected_id: Window from the pre-open snapshot.

        Returns:
            A window snapshot, or None. Caller cancellation propagates.
        """
        if budget <= 0:
            return None
        task = asyncio.create_task(self._window_rpc(page, token, minimize, expected_id))
        self._window_jobs.add(task)
        task.add_done_callback(self._window_done)
        try:
            done, _ = await asyncio.wait((task,), timeout=budget)
            if done:
                return task.result()
            self._window_warning()
        except asyncio.CancelledError:
            raise
        finally:
            if not task.done():
                self._window_generation += 1
                task.cancel()
        return None

    async def _open_owned_page(self, task_id, deadline):
        """Open and register a real Page before optional display changes.

        Args:
            task_id: Existing task or private inspection owner.
            deadline: Original operation deadline; display never borrows disposal time.

        Returns:
            The registered Page. Optional control failures leave normal behavior.
        """
        loop = asyncio.get_running_loop()
        self._window_generation += 1
        budget = min(
            WINDOW_CONTROL_SECONDS,
            max(0, (deadline - loop.time() - WINDOW_RESERVE_SECONDS) * 0.02),
        )
        enabled = (
            self.start_minimized
            and os.name == "nt"
            and not self._window_warned
            and budget > 0
        )
        coordinated = False
        snapshot = None
        if enabled:
            if not self._window_lock.locked():
                await self._window_lock.acquire()
                coordinated = True
        token = self._window_generation
        try:
            if coordinated and self.context.pages:
                started = loop.time()
                snapshot = await self._window_call(self.context.pages[0], budget, token)
                budget -= loop.time() - started
            opening = self._opening.get(task_id)
            if opening is None:
                opening = asyncio.create_task(self.context.new_page())
                self._opening[task_id] = opening
            page = await asyncio.shield(opening)
            self._pages[task_id] = page
            if (
                snapshot
                and snapshot[1] == "minimized"
                and not self._window_warned
                and token == self._window_generation
            ):
                await self._window_call(
                    page,
                    min(budget, deadline - loop.time() - WINDOW_RESERVE_SECONDS),
                    token,
                    minimize=True,
                    expected_id=snapshot[0],
                )
            return page
        finally:
            if coordinated:
                self._window_lock.release()

    def _load_records(self):
        """Reject untrusted ownership files without overwriting their bytes.

        Returns:
            None. Invalid files leave the instance paused for cleanup.
        """
        if not self._registry_path.exists():
            return
        try:
            original = self._registry_path.read_bytes()
            payload = json.loads(original.decode("utf-8"))
            if (
                not isinstance(payload, dict)
                or set(payload) != {"version", "sessions"}
                or type(payload["version"]) is not int
                or payload["version"] not in (1, 2)
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
            extra = {
                "cleanup_phase",
                "cleanup_reason",
                "cleanup_updated_at",
                "cleanup_resolution",
            }
            for task_id, record in payload["sessions"].items():
                if (
                    not isinstance(task_id, str)
                    or not task_id
                    or len(task_id) > 200
                    or not isinstance(record, dict)
                    or set(record)
                    != (fields if payload["version"] == 1 else fields | extra)
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
                    or record["submission"] != "confirmed"
                    or record["url"] != self._site.conversation_url(conversation_id)
                ):
                    raise ValueError
                if payload["version"] == 1:
                    record.update(
                        cleanup_phase=(
                            "not_required"
                            if record["keep"]
                            or record["cleanup"] in ("retained", "not_created")
                            else "confirmed"
                            if record["cleanup"] == "deleted"
                            else "legacy_unknown"
                        ),
                        cleanup_reason="legacy_record",
                        cleanup_updated_at=record["created_at"],
                        cleanup_resolution=None,
                    )
                if (
                    record["cleanup_phase"]
                    not in (
                        "not_required",
                        "not_started",
                        "delete_requested",
                        "confirmed",
                        "legacy_unknown",
                    )
                    or not isinstance(record["cleanup_reason"], str)
                    or re.fullmatch(r"[a-z_]{0,80}", record["cleanup_reason"]) is None
                    or datetime.fromisoformat(record["cleanup_updated_at"]).tzinfo
                    is None
                    or (
                        record["cleanup_phase"] == "not_required"
                        and not record["keep"]
                        and record["cleanup"] != "not_created"
                    )
                    or (
                        record["cleanup"] == "deleted"
                        and record["cleanup_phase"] != "confirmed"
                    )
                ):
                    raise ValueError
                resolution = record["cleanup_resolution"]
                if resolution is not None and (
                    not isinstance(resolution, dict)
                    or set(resolution) != {"actor_id", "resolved_at", "action"}
                    or resolution["action"] != "acknowledged"
                    or not isinstance(resolution["actor_id"], str)
                    or re.fullmatch(r"[0-9]{1,20}", resolution["actor_id"]) is None
                    or not any(c != "0" for c in resolution["actor_id"])
                    or datetime.fromisoformat(resolution["resolved_at"]).tzinfo is None
                ):
                    raise ValueError
            self._records = payload["sessions"]
            if payload["version"] == 1:
                self._migration_source = original
        except (OSError, ValueError, TypeError, KeyError, BrowserError):
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
            if self._migration_source is not None:
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    dir=self._registry_path.parent,
                    prefix=self._registry_path.name + ".v1-backup-",
                    suffix=".json",
                    delete=False,
                ) as backup:
                    backup.write(self._migration_source)
                    backup.flush()
                    os.fsync(backup.fileno())
                self._migration_source = None
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self._registry_path.parent,
                prefix=".owned_sessions-",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump({"version": 2, "sessions": self._records}, handle)
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

    async def _check_page(self, page, *, structure=False):
        """Delegate visible page checks to the selected site adapter."""
        await self._site.check_page(page, structure=structure)

    async def _wait_page_ready(self, page):
        """Wait for visible controls within both local and caller deadlines.

        Args:
            page: Owned page after navigation or opening a new chat.

        Raises:
            BrowserError: A definite fault appears or controls remain unknown.
            asyncio.CancelledError: The owner cancels its bounded operation.
        """
        deadline = asyncio.get_running_loop().time() + PAGE_READY_TIMEOUT_SECONDS
        while True:
            try:
                await self._check_page(page, structure=True)
                return
            except BrowserError as error:
                if error.category != "structure":
                    raise
                if asyncio.get_running_loop().time() >= deadline:
                    raise
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    async def resume(self) -> BrowserStatus:
        """Check residual ownership and visible login before opening admission.

        Returns:
            READY only after every ownership and readiness check succeeds.
        """
        async with self._operations:
            if (
                self._closing
                or self._resuming
                or self._maintenance_task is not None
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
                if self.cleanup_issues():
                    self._pause("cleanup")
                    return self.status
                if any(not task.done() for task in self._late_closers.values()):
                    self._pause("cleanup")
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

    def cleanup_issues(self) -> tuple[CleanupIssue, ...]:
        """Return unresolved ownership metadata without page content or I/O."""
        return tuple(
            CleanupIssue(key, record["cleanup_phase"], record["cleanup_reason"])
            for key, record in sorted(self._records.items())
            if record["cleanup"] not in ("deleted", "retained", "not_created")
            and record["cleanup_phase"] != "not_required"
            and record["cleanup_resolution"] is None
        )

    @property
    def cleanup_problem(self) -> str:
        """Expose a fixed diagnostic code, never a registry path or UI text."""
        if not self._registry_valid:
            return "registry_invalid"
        if self._records_dirty:
            return "record_write"
        if (
            self._pages
            or self._opening
            or any(not task.done() for task in self._cleanups.values())
            or any(not task.done() for task in self._late_closers.values())
        ):
            return "resource_pending"
        return "review_required" if self.cleanup_issues() else ""

    async def open_home(self) -> None:
        """Open only the dedicated login home, without replaying saved tasks.

        Raises:
            BrowserError: The context is closed or an earlier page is unresolved.
        """
        async with self._lock:
            if self._closing or self.context is None:
                raise BrowserError("closed")
            if self._home_page is not None and not self._home_page.is_closed():
                return
            if self._home_key in self._opening or self._home_key in self._pages:
                raise BrowserError("busy")
            deadline = asyncio.get_running_loop().time() + START_TIMEOUT_SECONDS
            page = None
            try:
                page = await asyncio.wait_for(
                    self._open_owned_page(
                        self._home_key, deadline - START_TIMEOUT_SECONDS * 0.2
                    ),
                    START_TIMEOUT_SECONDS * 0.8,
                )
                remaining = deadline - asyncio.get_running_loop().time()
                await asyncio.wait_for(
                    page.goto(self.base_url, wait_until="domcontentloaded"),
                    max(0, remaining * 0.9),
                )
                self._home_page = page
                self._opening.pop(self._home_key, None)
                self._pages.pop(self._home_key, None)
            finally:
                if self._home_page is not page or page is None:
                    await self._dispose_page(self._home_key, deadline)
            self._pause("cleanup" if self.cleanup_problem else "config")

    @asynccontextmanager
    async def _maintenance(self, task_id):
        """Reserve one administrator operation until its resources are joined.

        Args:
            task_id: Exact registered owner, never a URL or prefix.

        Raises:
            BrowserError: Invalid ownership, storage, or outstanding work.
        """
        async with self._operations:
            if not self._registry_valid or self._records_dirty:
                raise BrowserError("cleanup")
            if not isinstance(task_id, str) or task_id not in self._records:
                raise BrowserError("not_registered")
            if (
                self._closing
                or self._resuming
                or self._maintenance_task is not None
                or self._asks
                or self._pages
                or self._opening
                or any(not task.done() for task in self._cleanups.values())
                or any(not task.done() for task in self._late_closers.values())
                or self.status == BrowserStatus.READY
            ):
                raise BrowserError("busy")
            self._maintenance_task = asyncio.current_task()
            self._pause("cleanup")
        try:
            yield self._records[task_id]
        except BrowserError as error:
            if error.category in (
                "login",
                "challenge",
                "rate_limit",
                "unsupported_region",
            ):
                self._pause(error.category)
            raise
        finally:
            self._maintenance_task = None

    async def resolve_cleanup(self, task_id: str, actor_id: str) -> None:
        """Acknowledge one reviewed issue without rewriting its cleanup result.

        Args:
            task_id: Exact registered task ID.
            actor_id: Authorized administrator ID checked by the caller.

        Raises:
            BrowserError: The issue is active, invalid, or cannot be persisted.
        """
        if (
            not isinstance(actor_id, str)
            or re.fullmatch(r"[0-9]{1,20}", actor_id) is None
            or not any(c != "0" for c in actor_id)
        ):
            raise BrowserError("invalid_actor")
        async with self._maintenance(task_id) as record:
            if record["cleanup_resolution"] is not None:
                return
            if not any(issue.task_id == task_id for issue in self.cleanup_issues()):
                raise BrowserError("already_cleaned")
            record["cleanup_resolution"] = {
                "actor_id": actor_id,
                "resolved_at": datetime.now(timezone.utc).isoformat(),
                "action": "acknowledged",
            }
            try:
                self._save_records()
            except BrowserError:
                record["cleanup_resolution"] = None
                raise
            self._recovery.discard(task_id)

    async def retry_cleanup(self, task_id: str) -> CleanupResult:
        """Retry only a known task whose final delete was never attempted.

        Args:
            task_id: Exact registered task ID.

        Returns:
            The independently recorded cleanup outcome.

        Raises:
            BrowserError: Review is required, the browser is busy, or unavailable.
        """
        async with self._maintenance(task_id) as record:
            if (
                record["cleanup_phase"] != "not_started"
                or record["cleanup_resolution"] is not None
            ):
                raise BrowserError("review_required")
            if self.context is None:
                raise BrowserError("browser")
            self._cleanups[task_id] = asyncio.create_task(self._cleanup_owned(task_id))
            return await asyncio.shield(self._cleanups[task_id])

    async def check_cleanup(self, task_id: str) -> CleanupCheckResult:
        """Inspect one exact owned URL without deletion, submission, or writes.

        Args:
            task_id: Exact registered task ID.

        Returns:
            Available, unavailable, or unknown; unavailable never means deleted.

        Raises:
            BrowserError: The account or browser cannot be safely inspected.
        """
        async with self._maintenance(task_id):
            deadline = asyncio.get_running_loop().time() + CLEANUP_TIMEOUT_SECONDS
            try:
                return await asyncio.wait_for(
                    self._check_cleanup_page(task_id), CLEANUP_TIMEOUT_SECONDS * 0.9
                )
            except asyncio.TimeoutError:
                return CleanupCheckResult(task_id, "unknown")
            finally:
                disposed = await self._dispose_page(self._check_key, deadline)
                if not disposed:
                    self._pause("cleanup")
                    raise BrowserError("resource_pending")

    async def _check_cleanup_page(self, task_id):
        """Read visible controls on a disposable inspection page.

        Args:
            task_id: Validated owner whose address may be inspected.

        Returns:
            A content-free inspection result, never proof of deletion.
        """
        record = self._records[task_id]
        if record["url"] is None:
            return CleanupCheckResult(task_id, "unknown")
        if self.context is None:
            raise BrowserError("browser")
        page = await self._open_owned_page(
            self._check_key,
            asyncio.get_running_loop().time() + CLEANUP_TIMEOUT_SECONDS * 0.9,
        )
        await page.goto(record["url"], wait_until="domcontentloaded")
        try:
            await self._wait_page_ready(page)
        except BrowserError as error:
            if error.category != "structure":
                raise
            return CleanupCheckResult(task_id, "unknown")
        if page.url != record["url"]:
            return CleanupCheckResult(task_id, "unavailable")
        if self.provider == "deepseek":
            visible = await page.locator(".ds-message:visible").count()
        else:
            visible = (
                await page.locator(USER).count()
                or await page.locator(ASSISTANT).count()
            )
        return CleanupCheckResult(task_id, "available" if visible else "unknown")

    async def _probe_ready(self):
        """Inspect a disposable owned page inside resume's total probe budget.

        Raises:
            BrowserError: The visible login or required controls are unavailable.
        """
        probe = await self._open_owned_page(
            self._probe_key,
            asyncio.get_running_loop().time() + CLEANUP_TIMEOUT_SECONDS * 0.9,
        )
        await probe.goto(self.base_url, wait_until="domcontentloaded")
        await self._wait_page_ready(probe)

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
                or self._maintenance_task is not None
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
                "cleanup_phase": "not_started",
                "cleanup_reason": "",
                "cleanup_updated_at": datetime.now(timezone.utc).isoformat(),
                "cleanup_resolution": None,
            }
            self._records[task_id] = record
            self._recovery.add(task_id)
            self._save_records()
            self._asks[task_id] = asyncio.current_task()
        try:
            remaining = deadline - asyncio.get_running_loop().time()
            answer = await asyncio.wait_for(
                self._ask_page(task_id, prompt, deadline), remaining
            )
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
            site_category = (
                category
                if category
                in ("login", "challenge", "rate_limit", "unsupported_region")
                else None
            )
            if timed_out:
                category = "timeout"
            if record["submission"] == "attempted":
                category = "submission_unknown"
            if site_category:
                self._pause(site_category)
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
                category,
                submission_unknown=record["submission"] == "attempted",
                site_category=site_category,
            ) from None
        finally:
            self._asks.pop(task_id, None)

    async def _ask_page(self, task_id, prompt, deadline):
        """Run the linear UI flow within ask's single timeout budget.

        Args:
            task_id: Registered owner of the newly created page.
            prompt: User input to submit exactly once.
            deadline: Original generation deadline, including optional window work.

        Returns:
            A BrowserAnswer after the current assistant turn is complete.

        Raises:
            BrowserError: A visible failure or unsupported structure occurs.
        """
        if self.provider == "deepseek":
            return await self._ask_deepseek_page(task_id, prompt, deadline)
        record = self._records[task_id]
        page = await self._open_owned_page(task_id, deadline)
        if self._closing:
            raise BrowserError("closed")
        await page.goto(self.base_url, wait_until="domcontentloaded")
        await self._wait_page_ready(page)
        await page.locator(NEW_CHAT).first.click()
        await self._wait_page_ready(page)
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
            conversation_pending = False
            if record["submission"] == "confirmed":
                # URL changes alone cannot establish that this task owns a chat.
                conversation_pending = self._capture_conversation(record, page.url)
            await self._check_page(page)
            if (
                record["submission"] == "confirmed"
                and await page.locator(ASSISTANT).count() > assistants
            ):
                answer = page.locator(ASSISTANT).nth(assistants)
                markdown = answer.locator(ANSWER_BODY + ":visible")
                if not await answer.locator(ANSWER_BODY).count():
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
                            if not href:
                                continue
                            try:
                                parsed = urlsplit(href)
                                host = (parsed.hostname or "").rstrip(".").lower()
                                site_host = (
                                    (urlsplit(page.url).hostname or "")
                                    .rstrip(".")
                                    .lower()
                                )
                            except ValueError:
                                continue
                            if (
                                parsed.scheme in ("http", "https")
                                and host
                                and parsed.username is None
                                and parsed.password is None
                                and host != site_host
                                and not host.endswith("." + site_host)
                                and href not in parts[-1]
                            ):
                                parts.append(href)
                    text = "\n".join(parts).strip()
                    turn = answer.locator(
                        "xpath=ancestor::*[@data-talvt-turn-state][1]"
                    )
                    has_turn = await turn.count() > 0
                    stopped = not await page.locator(STOP).first.is_visible()
                    if has_turn:
                        stopped = stopped and (
                            await turn.get_attribute("data-talvt-turn-state")
                            == "complete"
                        )
                    complete = (
                        await (turn if has_turn else answer)
                        .locator(COPY)
                        .first.is_visible()
                    )
                    if text and stopped and not complete:
                        raise BrowserError("structure")
                    if text and stopped and complete and not conversation_pending:
                        now = asyncio.get_running_loop().time()
                        if stable_text != text:
                            stable_text, stable_since = text, now
                        elif now - stable_since >= STABLE_WINDOW_SECONDS:
                            return BrowserAnswer(text, record["conversation_id"])
                    else:
                        stable_text, stable_since = None, None
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    async def _ask_deepseek_page(self, task_id, prompt, deadline):
        """Submit one DeepSeek prompt and wait for a stable final answer."""
        record = self._records[task_id]
        page = await self._open_owned_page(task_id, deadline)
        if self._closing:
            raise BrowserError("closed")
        await page.goto(self.base_url, wait_until="domcontentloaded")
        await self._wait_page_ready(page)
        if page.url != self.base_url:
            raise BrowserError("structure")
        users, answers = await self._site.prepare_prompt(page, prompt)
        if users or answers:
            raise BrowserError("structure")
        record["submission"] = "attempted"
        self._save_records()
        await self._site.submit_prompt(page)
        stable_text, stable_since = None, None
        while True:
            snapshot = await self._site.inspect_turn(page, prompt, users, answers)
            if snapshot.submission_confirmed:
                if record["submission"] != "confirmed":
                    record["submission"] = "confirmed"
                    self._save_records()
                self._capture_conversation(record, page.url)
            # Default cleanup needs a canonical owned URL; some sites assign it
            # after the final text is already visible.
            if snapshot.complete and (record["conversation_id"] or record["keep"]):
                now = asyncio.get_running_loop().time()
                if stable_text != snapshot.final_text:
                    stable_text, stable_since = snapshot.final_text, now
                elif now - stable_since >= STABLE_WINDOW_SECONDS:
                    return BrowserAnswer(snapshot.final_text, record["conversation_id"])
            else:
                stable_text, stable_since = None, None
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    def _capture_conversation(self, record, url):
        """Capture a canonical URL only after the caller confirms its own prompt.

        Args:
            record: Current task ownership record.
            url: Visible URL of the task's own page.

        Returns:
            True while a known client placeholder still awaits a canonical URL.

        Raises:
            BrowserError: Origin or conversation identity is unsafe or changed.
        """
        if self.provider == "deepseek":
            conversation_id = self._site.parse_conversation_url(url)
            if conversation_id is None:
                return False
            if record["conversation_id"] not in (None, conversation_id):
                raise BrowserError("structure")
            if record["conversation_id"] is None:
                record["conversation_id"], record["url"] = conversation_id, url
                self._save_records()
            return False
        parsed = urlsplit(url)
        if parsed.hostname == "auth.openai.com":
            raise BrowserError("login")
        if f"{parsed.scheme}://{parsed.netloc}" != self._origin:
            raise BrowserError("browser")
        if not parsed.path.startswith("/c/"):
            return False
        conversation_id = parsed.path[3:]
        if (
            record["conversation_id"] is None
            and not parsed.query
            and not parsed.fragment
            and CLIENT_CONVERSATION_ID.fullmatch(unquote(conversation_id))
        ):
            return True
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
        return False

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
            async with self._cleanup_lock:
                # Queue time retains the caller's execution slot. Each active
                # deletion gets its own bounded UI and page-disposal budget.
                deadline = asyncio.get_running_loop().time() + CLEANUP_TIMEOUT_SECONDS
                status, reason = await asyncio.wait_for(
                    self._cleanup_page(task_id, record), CLEANUP_TIMEOUT_SECONDS * 0.9
                )
        except asyncio.TimeoutError:
            if record is not None and record["cleanup_phase"] == "not_started":
                status, reason = CleanupStatus.CLEANUP_FAILED, "precondition_timeout"
            else:
                status, reason = CleanupStatus.CLEANUP_UNKNOWN, "confirmation_timeout"
        except Exception as error:
            status = (
                CleanupStatus.CLEANUP_UNKNOWN
                if task_id in self._deleting
                else CleanupStatus.CLEANUP_FAILED
            )
            reason = (
                "confirmation_unknown"
                if task_id in self._deleting
                else "record_write"
                if isinstance(error, BrowserError) and error.category == "cleanup"
                else "browser"
            )
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
            record["cleanup_reason"] = reason
            record["cleanup_updated_at"] = datetime.now(timezone.utc).isoformat()
            if status in (CleanupStatus.RETAINED, CleanupStatus.NOT_CREATED):
                record["cleanup_phase"] = "not_required"
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
        if (
            record["cleanup_resolution"] is not None
            or record["cleanup_phase"] != "not_started"
        ):
            return CleanupStatus.CLEANUP_UNKNOWN, "review_required"
        if record["conversation_id"] is None:
            if record["submission"] == "not_attempted":
                return CleanupStatus.NOT_CREATED, "not_submitted"
            return CleanupStatus.CLEANUP_UNKNOWN, "conversation_unknown"
        if self.context is None:
            return CleanupStatus.CLEANUP_FAILED, "browser_closed"
        page = self._pages.get(task_id)
        if page is None or page.is_closed():
            page = await self._open_owned_page(
                task_id,
                asyncio.get_running_loop().time() + CLEANUP_TIMEOUT_SECONDS * 0.9,
            )
            if page.is_closed():
                return CleanupStatus.CLEANUP_FAILED, "page_closed"
        if page.url != record["url"]:
            await page.goto(record["url"], wait_until="domcontentloaded")
        if page.url != record["url"]:
            return CleanupStatus.CLEANUP_UNKNOWN, "conversation_mismatch"
        await self._wait_page_ready(page)
        if self.provider == "deepseek":
            await self._site.prepare_delete(page, record["url"])
            record["cleanup_phase"] = "delete_requested"
            record["cleanup_updated_at"] = datetime.now(timezone.utc).isoformat()
            try:
                self._save_records()
            except BrowserError:
                record["cleanup_phase"] = "not_started"
                raise
            self._deleting.add(task_id)
            await self._site.confirm_delete(page, record["url"])
            while True:
                try:
                    confirmed = await self._site.deletion_confirmed(page, record["url"])
                except BrowserError as error:
                    if error.category != "structure":
                        raise
                    confirmed = False
                if confirmed:
                    record["cleanup_phase"] = "confirmed"
                    return CleanupStatus.DELETED, "deleted"
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
        history_entry = page.locator(
            f'a[href="/c/{record["conversation_id"]}"]:visible'
        )
        history_seen = await history_entry.count() > 0
        while not history_seen:
            await self._check_page(page)
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            history_seen = await history_entry.count() > 0
        await (
            page.locator("main")
            .first.get_by_role(
                "button", name=re.compile(r"^(Conversation options|More)$")
            )
            .click()
        )
        await page.get_by_role("menuitem", name="Delete", exact=True).click()
        dialog = page.get_by_role("dialog", name=re.compile(r"^Delete chat\??$"))
        if page.url != record["url"]:
            return CleanupStatus.CLEANUP_UNKNOWN, "conversation_mismatch"
        record["cleanup_phase"] = "delete_requested"
        record["cleanup_updated_at"] = datetime.now(timezone.utc).isoformat()
        try:
            self._save_records()
        except BrowserError:
            record["cleanup_phase"] = "not_started"
            raise
        self._deleting.add(task_id)
        await dialog.get_by_role(
            "button", name=re.compile(r"^Delete(?: chat)?$")
        ).click()
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
            # The current UI reloads the empty home without a deletion toast.
            # Require the previously observed exact history entry to disappear
            # and the authenticated composer to finish loading after the modal.
            if (
                history_seen
                and page.url == self.base_url
                and not await dialog.is_visible()
                and not await history_entry.count()
            ):
                try:
                    await self._check_page(page, structure=True)
                except BrowserError as error:
                    if error.category != "structure":
                        raise
                else:
                    if (
                        not await page.locator(USER).count()
                        and not await page.locator(ASSISTANT).count()
                    ):
                        confirmed = True
            if confirmed and page.url != record["url"]:
                if not await history_entry.count():
                    record["cleanup_phase"] = "confirmed"
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
        self._home_page = None
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
