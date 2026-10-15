"""AstrBot lifecycle and command orchestration for the dedicated browser."""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Mapping
from types import SimpleNamespace
from uuid import uuid4

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .browser_chat import (
    BrowserAnswer,
    BrowserError,
    BrowserStatus,
    ChatGPTBrowser,
    CleanupResult,
    CleanupStatus,
)
from .messaging import (
    DEFAULT_ADMIN_TEMPLATE,
    DEFAULT_USER_TEMPLATE,
    SEND_TIMEOUT_SECONDS,
    DeliveryCancelled,
    DeliveryResult,
    DeliveryStatus,
    FailureNotifier,
    Fault,
    OneBotBinding,
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
from .scheduler import (
    CleanupState,
    Job,
    JobPhase,
    JobResult,
    ManagerState,
    SourceRoute,
    TaskManager,
)

COMMAND_PATTERN = (
    r"\A(?:/ask|ask) (?:ai(?=\s|\Z)[\s\S]*|status|resume(?: 已核验)?|cancel)\Z"
)
DEFAULT_CONFIG = {
    "restrict_groups": False,
    "allowed_groups": [],
    "restrict_users": False,
    "allowed_users": [],
    "max_concurrency": 1,
    "queue_capacity": 5,
    "execution_timeout_seconds": 180,
    "queue_timeout_seconds": 600,
    "max_reply_chars": 12000,
    "forward_node_chars": 1500,
    "max_input_chars": 12000,
    "keep_conversations": False,
    "admin_ids": [],
    "user_failure_template": DEFAULT_USER_TEMPLATE,
    "admin_failure_template": DEFAULT_ADMIN_TEMPLATE,
    "browser_channel": "chrome",
}


class ConfigError(ValueError):
    """A configuration key is invalid; never expose its supplied value."""


def validate_config(config: Mapping) -> dict:
    """Detach and validate the plugin configuration before creating resources.

    Args:
        config: AstrBotConfig or a plain mapping; omitted keys use schema defaults.

    Returns:
        A detached JSON-like snapshot; ID sequences become immutable tuples.

    Raises:
        ConfigError: With an invalid field name, never a value or template body.
    """
    if not isinstance(config, Mapping):
        raise ConfigError("config")
    result = {key: config.get(key, default) for key, default in DEFAULT_CONFIG.items()}
    for key in ("restrict_groups", "restrict_users", "keep_conversations"):
        if type(result[key]) is not bool:
            raise ConfigError(key)
    for key in ("allowed_groups", "allowed_users", "admin_ids"):
        values = result[key]
        if not isinstance(values, (list, tuple)) or any(
            not isinstance(value, str)
            or not value.isascii()
            or not value.isdecimal()
            or not any(char != "0" for char in value)
            for value in values
        ):
            raise ConfigError(key)
        result[key] = tuple(dict.fromkeys(values))
    for key in (
        "max_concurrency",
        "queue_capacity",
        "max_input_chars",
        "max_reply_chars",
        "forward_node_chars",
    ):
        if type(result[key]) is not int or result[key] < (
            0 if key == "queue_capacity" else 1
        ):
            raise ConfigError(key)
    for key in ("execution_timeout_seconds", "queue_timeout_seconds"):
        value = result[key]
        try:
            valid = type(value) in (int, float) and math.isfinite(value) and value > 0
        except OverflowError:
            valid = False
        if not valid:
            raise ConfigError(key)
    try:
        split_answer("", result["max_reply_chars"], result["forward_node_chars"])
    except ValueError:
        raise ConfigError("max_reply_chars/forward_node_chars") from None
    for key in ("user_failure_template", "admin_failure_template"):
        try:
            validate_template(result[key])
        except TemplateError:
            raise ConfigError(key) from None
    if result["browser_channel"] not in ("chrome", "msedge"):
        raise ConfigError("browser_channel")
    return result


class BrowserChatPlugin(Star):
    """ChatGPT browser chat: /ask ai, status, resume and own-task cancel."""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context, config)
        self._raw_config = config
        self.settings = None
        self.config_error = None
        self.browser = None
        self.manager = None
        self._bindings = {}
        self._notifiers = {}
        self._watchers = set()
        self._delivery = {}
        self._lifecycle = asyncio.Lock()
        self._initialized = False
        self._closing = False
        self._terminate_task = None

    async def initialize(self) -> None:
        """Create resources once; failed configuration/login stays diagnosably paused."""
        async with self._lifecycle:
            if self._initialized or self._closing:
                return
            try:
                self.settings = validate_config(self._raw_config)
            except ConfigError as error:
                self.config_error = str(error)
                self.logger.error(
                    "Invalid browser-chat configuration field: %s", self.config_error
                )
            settings = self.settings or validate_config({})
            self.manager = TaskManager(
                settings["max_concurrency"],
                settings["queue_capacity"],
                settings["queue_timeout_seconds"],
                self._run,
            )
            await self.manager.start()
            await self.manager.pause("config" if self.config_error else "starting")
            self._initialized = True
            if self.config_error:
                return
            # Startup faults have no event. Identify only enabled OneBot instances
            # using their public API, never a random client UUID or another bot.
            platform_manager = getattr(self.context, "platform_manager", None)
            for platform in platform_manager.get_insts() if platform_manager else ():
                if platform.meta().name != "aiocqhttp":
                    continue
                try:
                    client = platform.get_client()
                    identity = await asyncio.wait_for(
                        client.call_action("get_login_info"), 5
                    )
                    self._bind(
                        OneBotBinding(
                            platform.meta().id, str(identity["user_id"]), client
                        )
                    )
                except Exception as error:
                    self.logger.warning(
                        "OneBot startup identity unavailable: type=%s",
                        type(error).__name__,
                    )
            reason = await self._start_browser()
            if reason is None and not self._closing:
                try:
                    await self.browser.resume()
                    reason = self.browser.pause_reason or "config"
                except Exception as error:
                    self.logger.warning(
                        "Browser readiness check failed: type=%s", type(error).__name__
                    )
                    reason = "browser"
            await self.manager.pause(reason or "closed")
            if reason not in (None, "config"):
                for platform_id, notifier in self._notifiers.items():
                    await notifier.notify_failure(
                        platform_id, Fault("startup", None, reason, reason)
                    )

    def _bind(self, binding: OneBotBinding) -> None:
        """Retain one exact source account and one notifier per enabled instance."""
        existing = self._bindings.get(binding.platform_id)
        if existing is not None:
            if existing.self_id != binding.self_id:
                raise ValueError("Source bot identity changed; reload required")
            return
        self._bindings[binding.platform_id] = binding
        self._notifiers[binding.platform_id] = FailureNotifier(
            {binding.platform_id: binding},
            self.settings["admin_ids"],
            admin_failure_template=self.settings["admin_failure_template"],
        )

    async def _start_browser(self):
        try:
            if self.browser is None:
                self.browser = ChatGPTBrowser(
                    StarTools.get_data_dir("astrbot_plugin_browser_chat"),
                    channel=self.settings["browser_channel"],
                )
            await self.browser.start()
            return None
        except Exception as error:
            dependency = isinstance(error, ImportError) or (
                isinstance(error, RuntimeError)
                and "Playwright is missing" in str(error)
            )
            reason = "dependency" if dependency else "browser"
            self.logger.warning(
                "Browser startup failed: category=%s type=%s",
                reason,
                type(error).__name__,
            )
            return reason

    async def _reply(self, event, text):
        try:
            # event.send succeeds with None; neither falsiness nor a timeout is
            # grounds for retrying this acknowledgement through another route.
            await asyncio.wait_for(
                event.send(event.plain_result(text)), SEND_TIMEOUT_SECONDS
            )
        except Exception as error:
            self.logger.warning(
                "Command acknowledgement unconfirmed: type=%s", type(error).__name__
            )

    @filter.regex(COMMAND_PATTERN)
    async def on_command(self, event: AstrMessageEvent):
        """Claim precise commands before awaiting; stop the default LLM pipeline.

        Args:
            event: Source aiocqhttp event. Background jobs keep only its binding.
        """
        text = event.get_message_str().strip()
        if not re.fullmatch(COMMAND_PATTERN, text) or (
            text.startswith("ask ") and event.is_at_or_wake_command is not True
        ):
            return
        event.stop_event()
        if event.get_platform_name() != "aiocqhttp":
            await self._reply(event, "此插件仅支持 OneBot 来源。")
            return
        command = text.split(maxsplit=2)[1]
        settings = self.settings or validate_config({})
        admin = (
            event.is_admin() is True or event.get_sender_id() in settings["admin_ids"]
        )
        if command in ("status", "resume") and not admin:
            await self._reply(event, "没有管理权限。")
            return
        if command == "status":
            state = self.manager.snapshot() if self.manager else None
            reason = self.config_error or (state.pause_reason if state else "starting")
            await self._reply(
                event,
                "BrowserChat：{}；执行 {}；等待 {}；原因 {}。".format(
                    state.state.value if state else "starting",
                    state.running if state else 0,
                    state.queued if state else 0,
                    reason or "ready",
                ),
            )
            return
        if self._closing or not self._initialized or self.config_error:
            await self._reply(event, "服务未就绪，请管理员检查配置并重载本插件。")
            return
        if command == "resume":
            async with self._lifecycle:
                state = self.manager.snapshot()
                if (
                    self._closing
                    or state.running
                    or state.state in (ManagerState.CLOSING, ManagerState.CLOSED)
                ):
                    await self._reply(event, "旧任务仍在退出或清理，请稍后恢复。")
                    return
                await self.manager.pause("resuming")
                reason = await self._start_browser()
                if reason is None:
                    try:
                        status = await self.browser.resume(
                            account_settings_verified=text.endswith(" 已核验")
                        )
                        reason = (
                            None
                            if status == BrowserStatus.READY
                            else (self.browser.pause_reason or "browser")
                        )
                    except Exception as error:
                        self.logger.warning(
                            "Browser resume failed: type=%s", type(error).__name__
                        )
                        reason = "browser"
                if reason is None and not self._closing:
                    await self.manager.resume()
                    await self._reply(event, "网页就绪检查通过，已恢复接单。")
                else:
                    await self.manager.pause(reason or "closed")
                    await self._reply(
                        event,
                        "仍暂停（{}）。请在专用浏览器手动登录，关闭记忆、引用聊天历史和自定义指令；实际核验后使用 /ask resume 已核验。依赖缺失时请用宿主解释器安装 requirements.txt。".format(
                            reason or "closed"
                        ),
                    )
            return
        if command == "cancel":
            found = await self.manager.cancel(
                (event.get_platform_id(), event.get_sender_id())
            )
            await self._reply(
                event,
                "取消处理已结束；已提交或结果未知的消息不会撤回。"
                if found
                else "没有你的未完成任务。",
            )
            return
        if not check_access(event.get_sender_id(), event.get_group_id(), settings):
            await self._reply(event, "没有使用权限。")
            return
        try:
            binding = OneBotBinding.from_event(event)
            self._bind(binding)
            group = event.get_group_id()
            route = SourceRoute(
                binding.platform_id,
                "group" if group else "private",
                group or event.get_sender_id(),
                binding,
            )
            components = event.get_messages()
            current = "".join(item.text for item in components if item.type == "Plain")
            if event.is_at_or_wake_command is True and not current.startswith(
                ("/ask ai", "ask ai")
            ):
                host_config = self.context.get_config(umo=event.unified_msg_origin)
                for prefix in host_config.get("wake_prefix", []):
                    if prefix and current.startswith(prefix):
                        current = current[len(prefix) :].strip()
                        break
            prompt_event = SimpleNamespace(
                get_messages=lambda: [SimpleNamespace(type="Plain", text=current)]
                + [item for item in components if item.type == "Reply"],
                is_at_or_wake_command=event.is_at_or_wake_command,
            )
            prompt = extract_prompt(prompt_event, settings["max_input_chars"])
            message_id = event.message_obj.message_id
            if type(message_id) not in (str, int) or not str(message_id).strip():
                raise ValueError("Missing source message identity")
            job = Job(
                uuid4().hex,
                (
                    route.platform_id,
                    route.message_type,
                    route.target_id,
                    str(message_id),
                ),
                (route.platform_id, event.get_sender_id()),
                prompt,
                route,
                settings,
            )
            admission = await self.manager.submit(job)
        except PromptError as error:
            suffix = (
                f"（{error.actual_chars} / {error.limit_chars} 字符）"
                if error.code == "input_too_long"
                else ""
            )
            await self._reply(
                event,
                render_failure(
                    Fault("input", None, error.code, error.code),
                    template=settings["user_failure_template"],
                )
                + suffix,
            )
            return
        except Exception as error:
            self.logger.warning(
                "Command admission rejected: type=%s", type(error).__name__
            )
            await self._reply(event, "无法确认来源或解析消息，请管理员检查平台和配置。")
            return
        if not admission.accepted:
            if admission.reason == "duplicate_message":
                return
            await self._reply(
                event,
                render_failure(
                    Fault("admission", route, admission.reason, admission.reason),
                    template=settings["user_failure_template"],
                ),
            )
            return
        watcher = asyncio.create_task(self._watch(admission.task_id, route, settings))
        self._watchers.add(watcher)
        watcher.add_done_callback(self._watchers.discard)
        await self._reply(
            event,
            f"任务 {admission.task_id} 已接收，前方等待 {admission.queue_position}。",
        )

    def _record_delivery(self, job, delivery: DeliveryResult):
        self._delivery[job.task_id] = delivery
        if delivery.status != DeliveryStatus.CONFIRMED:
            self.logger.warning(
                "Answer delivery incomplete: task=%s status=%s confirmed_parts=%d total_parts=%d uncertain=%s",
                job.task_id,
                delivery.status.value,
                delivery.confirmed_parts,
                delivery.total_parts,
                delivery.uncertain,
            )
        if delivery.status == DeliveryStatus.CONFIRMED:
            job.result, job.reason = JobResult.SUCCESS, None
        elif delivery.uncertain or delivery.status == DeliveryStatus.UNKNOWN:
            job.result, job.reason = JobResult.DELIVERY_UNKNOWN, "delivery_unknown"
        else:
            job.result, job.reason = (
                JobResult.FAILED,
                "partial" if delivery.confirmed_parts else "delivery",
            )

    async def _run(self, job: Job):
        """Own independent execution/delivery budgets and finally cleanup.

        Args:
            job: Scheduler-owned request and immutable configuration snapshot.
        """
        try:
            if job.cancel_requested or self._closing:
                raise asyncio.CancelledError
            generation = asyncio.create_task(
                self.browser.ask(
                    job.task_id,
                    job.prompt,
                    job.config["execution_timeout_seconds"],
                    keep=job.config["keep_conversations"],
                )
            )
            try:
                done, _ = await asyncio.wait(
                    {generation}, timeout=job.config["execution_timeout_seconds"]
                )
                if generation not in done:
                    raise asyncio.TimeoutError
                answer: BrowserAnswer = generation.result()
            finally:
                if not generation.done():
                    generation.cancel()
                await asyncio.gather(generation, return_exceptions=True)
            if job.cancel_requested or self._closing:
                raise asyncio.CancelledError
            parts = split_answer(
                answer.text,
                job.config["max_reply_chars"],
                job.config["forward_node_chars"],
            )
            job.phase = JobPhase.DELIVERING
            delivery = await send_answer(
                job.route, parts, timeout_s=SEND_TIMEOUT_SECONDS
            )
            self._record_delivery(job, delivery)
        except DeliveryCancelled as error:
            self._record_delivery(job, error.result)
            if (
                not error.result.uncertain
                and error.result.status != DeliveryStatus.CONFIRMED
            ):
                job.result, job.reason = JobResult.CANCELLED, "cancelled"
            raise asyncio.CancelledError from None
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            job.result, job.reason = JobResult.TIMED_OUT, "timeout"
        except BrowserError as error:
            job.result = (
                JobResult.TIMED_OUT if error.category == "timeout" else JobResult.FAILED
            )
            job.reason = error.category
            await self.manager.pause(self.browser.pause_reason or error.category)
        except Exception as error:
            job.result, job.reason = JobResult.FAILED, "browser"
            self.logger.warning("Browser job failed: type=%s", type(error).__name__)
            await self.manager.pause("browser")
        finally:
            job.phase = JobPhase.CLEANING
            try:
                # Browser cleanup owns its 20-second deadline and late-page
                # tracking. A second outer timeout could release the slot early.
                cleanup: CleanupResult = await self.browser.cleanup(job.task_id)
                job.cleanup = CleanupState(cleanup.status.value)
            except Exception as error:
                job.cleanup = CleanupState(CleanupStatus.CLEANUP_UNKNOWN.value)
                self.logger.warning(
                    "Browser cleanup unconfirmed: type=%s", type(error).__name__
                )
            if job.cleanup in (
                CleanupState.CLEANUP_FAILED,
                CleanupState.CLEANUP_UNKNOWN,
            ):
                await self.manager.pause("cleanup")
            elif self.browser.status == BrowserStatus.PAUSED:
                await self.manager.pause(self.browser.pause_reason or "browser")

    async def _watch(self, task_id, route, settings):
        """Report one terminal outcome, including jobs that never entered a runner."""
        try:
            outcome = await self.manager.wait(task_id)
            if outcome.result != JobResult.SUCCESS:
                reason = outcome.reason or outcome.result.value
                category = (
                    (self.manager.snapshot().pause_reason or "paused")
                    if reason == "paused"
                    else reason
                )
                fault = Fault(task_id, route, category, reason)
                await send_failure(
                    route,
                    fault,
                    template=settings["user_failure_template"],
                    timeout_s=SEND_TIMEOUT_SECONDS,
                )
                if outcome.result != JobResult.CANCELLED:
                    await self._notifiers[route.platform_id].notify_failure(
                        route.platform_id, fault
                    )
            if outcome.cleanup in (
                CleanupState.CLEANUP_FAILED,
                CleanupState.CLEANUP_UNKNOWN,
            ):
                await self._notifiers[route.platform_id].notify_failure(
                    route.platform_id,
                    Fault(task_id, route, "cleanup", outcome.cleanup.value),
                )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.logger.warning(
                "Task outcome notice failed: type=%s", type(error).__name__
            )
        finally:
            self._delivery.pop(task_id, None)

    async def terminate(self) -> None:
        """Join owned work on unload; concurrent calls share the same shutdown."""
        self._closing = True
        if self._terminate_task is None:
            self._terminate_task = asyncio.create_task(self._terminate_owned())
        await asyncio.shield(self._terminate_task)

    async def _terminate_owned(self):
        async with self._lifecycle:
            try:
                if self.manager:
                    await self.manager.close()
                if self.browser:
                    await self.browser.close()
            finally:
                await asyncio.gather(*tuple(self._watchers), return_exceptions=True)
                await asyncio.gather(
                    *(item.close() for item in self._notifiers.values()),
                    return_exceptions=True,
                )
