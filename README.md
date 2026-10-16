# astrbot_plugin_browser_chat

计划通过专用浏览器操作 ChatGPT 网页，将文字回答返回 QQ 的 AstrBot 插件。后续考虑 Gemini 和 DeepSeek 适配。

**当前状态：Task 5 验收进行中。完整单元及 Chrome 本地页面测试 134/134 通过，Ruff 和隔离装载检查通过；真实 ChatGPT＋QQ 主流程尚未通过，不能声明真实接入可用。**

开发前阅读 [AGENTS.md](AGENTS.md) 和 [完整方案与开发交接](docs/superpowers/plans/2026-10-15-browser-chat-handoff.md)。本次仅验收及修复本插件；真实消息仅限用户明确指定的来源机器人、测试群、私聊与管理员。下面历史开发阶段的测试数不替代本轮完整结果。

## 安装与首次使用

1. 将私有仓库放在 `AstrBot/data/plugins/astrbot_plugin_browser_chat`。已有安装保留 Git 修改、插件配置和专用 profile；本插件暂为开发版 `0.1.0`，不代表已通过市场或真实平台验收。
2. 使用**实际运行 AstrBot 的 Python** 安装本插件 requirements，不升级宿主或其他插件。当前范围 `playwright>=1.58.0,<1.61`，调用系统 Chrome，无需下载 Playwright 自带浏览器。导入模块不会安装依赖。

   ```powershell
   & '<实际宿主的 python.exe>' -m pip install -r '<插件目录>\requirements.txt'
   ```

3. 在 AstrBot WebUI 配置并仅加载/重载本插件。先设置 `admin_ids`；测试阶段建议同时启用群、用户白名单，仅填写获准的对象。空管理员列表会警告，不能当作通知成功。缺依赖或配置错误时保持暂停，通过 `/ask status` 和脱敏日志诊断。
4. 在自动打开的专用 Chrome/Edge 中手动访问 `https://chatgpt.com/` 并登录。资料目录位于宿主 `data/plugin_data/astrbot_plugin_browser_chat/`：Chrome 默认 `browser_profile`，Edge 默认 `browser_profile_msedge`；也可用 `browser_profile_name` 指定该根目录内的专用目录名称。与日常浏览器分离；不要把 Cookie、profile 或登录凭据放进 Git。
5. 在同一账号实际关闭跨会话记忆、引用聊天历史和个人自定义指令，再由管理员发送 `/ask resume 已核验`。该命令是人工确认，不会自动读取或证明这些设置。换号、重新登录后重新核验。普通 `/ask resume` 不会代替人工确认。
6. 确认状态就绪后使用 `/ask ai 问题`。每次问题创建独立网页会话，默认完成后定向删除；保留策略在任务准入时固定。遇到验证码、登录失效、限流或无法识别的页面会暂停，不绕过限制、不自动重复提交不确定的问题。

本插件只管理自己创建的会话和专用浏览器。QQ 已确认接受或结果未知的消息不会因取消而撤回；会话清理失败独立记录和通知，不把已经成功的答案重发。

## 配置

所有字段与 [_conf_schema.json](_conf_schema.json) 一致。结构配置通过本插件重载生效；已接纳任务使用快照。

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `restrict_groups` / `allowed_groups` | false / [] | 独立群白名单，启用且空列表时拒绝所有群 |
| `restrict_users` / `allowed_users` | false / [] | 独立用户白名单；群聊取交集，私聊只检查用户 |
| `max_concurrency` | 1 | 正整数；大于 1 的真实平台表现需额外验收 |
| `queue_capacity` | 5 | 非负整数；0 表示不排队 |
| `execution_timeout_seconds` | 180 | 网页生成预算，不包含发送或清理 |
| `queue_timeout_seconds` | 600 | 等待截止时间 |
| `max_input_chars` | 12000 | 当前文字＋直接引用合并后超限拒绝 |
| `max_reply_chars` / `forward_node_chars` | 12000 / 1500 | 先截断并计入提示，再按段落分段；节点不超过总预算 |
| `keep_conversations` | false | 是否保留插件自建会话，不改变已接纳任务的策略 |
| `admin_ids` | [] | QQ 字符串 ID；接收通知且可执行 status/resume，宿主超级管理员也有管理权限 |
| `user_failure_template` | `任务 {task_id} 失败：{reason}。` | 用户失败提示 |
| `admin_failure_template` | 分类、任务、来源、时间、原因、次数 | 独立管理员模板，默认无正文 |
| `browser_channel` | chrome | chrome/msedge；本机 Edge 已通过人工登录和 Playwright 保留登录访问检查，问答/清理另行验收 |
| `browser_profile_name` | 空字符串 | 按浏览器选择默认目录；显式名称限 1—64 位 ASCII 字母、数字、下划线、短横线，拒绝 Windows 保留名称和路径 |

本机已验证的专用 Edge 配置为 `browser_channel="msedge"`、`browser_profile_name="edge_manual_diagnostic"`，直接复用插件自己的登录目录；不复制个人浏览器资料。旧 `browser_profile` 继续使用 `owned_sessions.json`，其他目录使用 `owned_sessions.<目录名>.json`，恢复清理仅处理所选目录的归属记录。不同进程不能同时占用同一目录；先正常退出本插件的诊断窗口再启动正式插件。浏览器或资料配置修改通过插件重载生效，账号设置仍需人工核验。

模板只允许 `{task_id}`、`{reason}`、`{category}`、`{source}`、`{time}`、`{count}`，不支持属性、下标、转换、非空格式说明或表达式。非法配置不会静默执行。管理员通知按来源平台、浏览器 scope 和故障类别合并 300 秒：首条立即，后续累计，有新增时窗口末补一次汇总；通知失败只记录日志，不递归通知。

## 本轮验收状态

用户已明确指定 Salmon 及测试群/私聊/管理员，具体号码仅用于获准的实时验收，不写入仓库示例配置。2026-10-16 用户确认：专用 Edge 登录成功、Playwright 重开后保留登录且可访问，并已实际关闭记忆、引用聊天历史和自定义指令。原 Chrome 的 CF 根因尚未确定；以上仅证明登录访问，真实问答、会话清理及 QQ 仍待验收。目前尚未向真实 QQ 发送验收消息。

| 场景 | 本轮自动验证 | 真实平台 |
| --- | --- | --- |
| 普通提问与单次回答 | 通过：模拟入口＋可控页面 | 未执行：待登录与宿主就绪 |
| 引用＋问题、仅引用、引用读取失败 | 通过：文字链与错误边界 | 未执行：待指定 QQ 实测 |
| 群/用户白名单及私聊规则 | 通过：四种开关组合与入口权限 | 未执行：待指定范围抽样 |
| 1执行＋5等待＋1拒绝、跨来源用户占位 | 通过：受控并发 | 未执行：不制造大量真实请求 |
| 执行/排队超时、取消、清理占槽 | 通过：受控协程/页面/集成 | 不故意在真实账号制造异常 |
| 长答案、代码、截断、分段 | 通过：Unicode/预算/稳定长前缀 | 未执行：待一条真实长答案 |
| 群/私聊合并转发 | 通过：OneBot 替身及实际 SDK语义 | 未执行：待真实 QQ 展示确认 |
| 明确拒绝降级、超时未知、部分成功 | 通过：接口替身；不重发全文/已确认段 | 不故意制造真实发送故障 |
| 登录失效、验证码、限流、结构改变 | 通过：可控页面；暂停及安全恢复 | 不故意触发风控 |
| 管理员多接收者、错平台、异常、合并 | 通过：接口替身及受控 300 秒窗口 | 未执行：待获准私信可达性验证 |
| 删除、保留、重启残留及原子记录 | 通过：自建会话可控页面与文件测试 | 未执行：待真实自建会话验证 |
| 插件装载、卸载与新实例生命周期 | 通过：真实 API隔离检查 | 未执行：运行中插件热重载待就绪 |
| 并发 >1 | 通过：独立本地标签页 | 未执行：默认单并发，不扩展真实压力测试 |

已验证 Windows、Python 3.10.6、Chrome 155.0.8059.79、venv Playwright 1.58.0、aiocqhttp 1.4.4；另一已安装解释器的 Playwright 1.60.0 仅有历史生命周期证据，本轮未重复整套测试。当前源码的 AstrBot 为 4.28.0，声明 Python >=3.12；全局 3.10.6 仅通过隔离 API/过滤链装载，不能据此宣称官方支持 3.10。venv 缺宿主 loguru，未安装或升级宿主依赖。

2026-10-16 Edge 最小接入的针对性回归：资料目录 5 项、插件集成 24 项、生命周期 8 项通过；1 项旧可选 Chrome 用例跳过。临时 Edge 本地页面、取消和重开通过，Ruff 通过。以下 134 项为此前完整回归记录。

本轮完整测试显式包含旧的可选 Chrome 本地生命周期用例，134 项全部通过、无跳过，耗时 192.326 秒。Ruff check/format 通过；完整结果与后续真实验收边界见交接文档。复现命令只访问本地可控页面，使用临时 profile，不发送 QQ：

```powershell
$env:BROWSER_CHAT_REAL_TEST = '1'
try {
    & '..\..\..\venv\Scripts\python.exe' -m unittest discover -s tests -p 'test_*.py' -v
} finally {
    Remove-Item Env:BROWSER_CHAT_REAL_TEST
}
```

实际限制：生产 ChatGPT 选择器仍需登录后核验，网站语言/布局变化可能导致暂停；保留、删除及账号设置的本地通过不等于真实通过。API 确认接受不等于客户端已展示或用户已读；私信依赖指定机器人可联系管理员。程序不承诺网站额度、不绕过验证码/地区/限流限制。真实 ChatGPT＋QQ 主流程通过前保持“真实接入未通过”。

## Task 4 开发接口与历史检查

`main.py` 通过继承 Star 注册，导入不启动浏览器、不安装依赖。initialize 校验配置并启动专用可见浏览器，默认保持暂停；terminate 停止准入、取消并等待旧任务清理、关闭专用浏览器及全部通知任务，不定义 __del__。

| 命令 | 权限及行为 |
| --- | --- |
| `/ask ai 问题` | 满足启用的群/用户白名单；可直接引用文字，按快照进入有界队列 |
| `/ask cancel` | 仅取消调用者在同一平台的未完成任务，跨群/私聊共享；等待清理，已提交或结果未知的消息不会撤回 |
| `/ask status` | 宿主超级管理员或 admin_ids 中的插件管理员；只展示总计数和暂停原因 |
| `/ask resume` | 同上；检查旧任务已退出、网页就绪与残留，不自动确认账号设置 |
| `/ask resume 已核验` | 同上；仅在人工已登录，并关闭记忆、引用聊天历史及自定义指令后使用 |

`admin_ids` 同时用于管理员通知及 status/resume 授权；它不绕过 ai 的使用白名单。裸群文本 `ask ai` 不执行；宿主已通过前缀或 @ 唤醒后才接受剥离前缀的形式。本机 `#ask ai` 已在隔离的当前宿主过滤链验证。入口先 stop_event，再直接发送回执；不用 yield 回执，避免默认 LLM 重复响应。

首次加载后，在专用浏览器窗口手动打开 ChatGPT 并登录，实际核验账号设置，再发 `/ask resume 已核验`。缺 Playwright 时保持 dependency 暂停，使用实际宿主解释器安装 requirements.txt；插件不会自动安装，也不升级 AstrBot。配置无效时保持 config 暂停，只报告字段名，修正后重载本插件。全局唤醒、宿主白名单及插件会话启用规则仍然有效。

配置见 [_conf_schema.json](_conf_schema.json)，默认：公开使用，1 执行＋5 等待，每用户 1 个未完成任务；执行 180 秒、排队 600 秒；输入/总输出 12000 字符、节点 1500 字符；默认删除自建会话；管理员列表为空；Chrome。发送使用独立 30 秒总预算，清理由浏览器模块使用 20 秒预算。白名单、保留策略和模板使用准入快照；结构配置修改通过插件重载生效。

本轮验证：消息 50 项＋调度 15 项＋模拟插件集成 22 项，**87 项通过**。隔离 ASTRBOT_ROOT 子进程还验证了真实 AstrBot API 导入、schema/metadata、Star 注册、5 个命令经真实 waking/process 链识别、默认 LLM 调用为 0、真实管理器 async terminate 分支和新实例生命周期。浏览器、QQ、LLM 使用替身，会话启用策略在该探针中固定为允许；这不是运行中宿主热重载验收。

venv Python 3.10.6 缺宿主依赖 loguru，未补装或升级；另一已安装的全局 Python 3.10.6 可完成真实 API 隔离检查。宿主项目声明 Python >=3.12，独立检查不等于官方支持 3.10，也不能证明运行宿主的 sys.prefix/import 路径。

必要回归（排除未改动的浏览器套件）：

```powershell
& '..\..\..\venv\Scripts\python.exe' -m unittest discover -s tests -p 'test_[mps]*.py' -v
```

后续真实重载的实际影响：仅本插件停止接单，旧任务取消并等清理，可能向已有任务来源发送失败提示和管理员通知；专用浏览器关闭并重新初始化，人工设置确认需要重新完成。已提交的 QQ 消息不能撤回，其他插件及整个宿主不应重启。本次没有执行这项运行中操作。

## Task 3 发送与通知接口

- `OneBotBinding.from_event(event)` 捕获来源 aiocqhttp 实例 ID、机器人 QQ ID及原始客户端，放入 `SourceRoute.send_handle`。仅接受匹配的 platform_id、group/private 和有效目标 ID；每个请求携带原机器人 self_id，不能拿其他平台兜底。
- `await send_answer(route, parts, *, timeout_s=30) -> DeliveryResult`：parts 使用 split_answer 的输出。群/私聊均先发一条合并转发，只有明确拒绝后才进行一次普通分段发送；整个流程共享 30 秒预算。不确定时不降级，部分成功后不重发任何已确认节点。
- `DeliveryResult` 不含正文，字段为 status、confirmed_parts、total_parts、used_fallback、uncertain、reason。status 为 confirmed/partial/unknown/failed；partial 的 uncertain 标记当前失败段是否可能已发送。confirmed 仅表示接口接受，不表示 QQ 客户端已展示。
- `DeliveryCancelled.result` 保存取消时的进度。由于 Python 3.10 跨任务等待会丢失 CancelledError 子类字段，该类型是普通异常；main.py 必须先记录 result，再重新抛 `asyncio.CancelledError`，不得把它当普通可重试故障。
- `Fault(task_id, source, category, reason="", browser_account_scope="default", occurred_at=...)` 仅传故障元数据，reason 使用固定错误码；任意异常文本、页面内容和正文均不会进入渲染。occurred_at 默认 UTC发生时间，窗口汇总保留首条时间。
- `validate_template(template)` 校验六个简单占位符 task_id/reason/category/source/time/count；属性、下标、转换和非空格式说明均拒绝。`render_failure(fault, *, template=None, admin=False, count=1)` 对无效模板记录配置警告并使用默认安全文案，替换字段不能注入换行或 CQ 码。
- `await send_failure(route, fault, *, template=None, timeout_s=30)` 只给原用户发送一次普通失败提示，返回 DeliveryResult，不自动递归触发管理员通知。调用方传任务配置快照中的 user_failure_template。
- `FailureNotifier(bindings, admin_ids, *, admin_failure_template=...)` 复制配置；`await notifier.notify_failure(platform_id, fault)` 在来源机器人下分别私信管理员。按平台、浏览器账号 scope、故障类别合并 300 秒：首条立即发送，窗口末仅在有后续事件时补一次总次数汇总。失败只记录脱敏日志，不递归或自动重试；空管理员列表会警告，不能当作通知成功。
- `await notifier.close()` 取消并等待自身计时器和发送，不在卸载时额外发汇总。没有任务来源的启动故障由 main.py 显式枚举启用的 OneBot binding，分别传 source=None 的 Fault；不猜测默认平台。

发送直接使用 CQHttp 的 call_action，与当前宿主的 OneBot 请求结构一致。`event.send()` 成功返回 None 且丢弃原始数据，`Context.send_message()` 的 bool 只代表路由是否存在，均不用于推断原始 OneBot 投递。原始客户端返回 None/False、异步或未知响应时按 unknown 停止，不重复全文；ActionFailed/ApiNotAvailable 或明确失败响应才允许有限降级。

本轮仅执行消息和调度的必要测试，不打开浏览器或发送 QQ：

```powershell
& '..\..\..\venv\Scripts\python.exe' -m unittest discover -s tests -p 'test_messaging.py' -v
& '..\..\..\venv\Scripts\python.exe' -S -m unittest discover -s tests -p 'test_scheduler.py' -v
```

Task 3 无新增依赖。真实转发、普通消息及管理员私信验收留待明确测试对象后执行。Task 4 已接入结果映射、取消传播、用户提示、管理员通知和 notifier.close，当前集成验证见上文。

本轮必要回归结果：消息模块 49 项、调度 15 项全部通过；其中实际导入当前安装的 aiocqhttp 核验数据解包和明确拒绝异常，其余发送全部替身化。Ruff、语法及差异检查通过，未重跑浏览器，也未进行真实 QQ 或宿主加载验收。

## Task 2 浏览器接口

`browser_chat.py` 不依赖 AstrBot 事件对象。后续入口传入 `StarTools.get_data_dir("astrbot_plugin_browser_chat")`，仅使用该目录下专用 profile；不使用日常浏览器资料。

- `ChatGPTBrowser(data_dir: Path, channel="chrome", *, base_url="https://chatgpt.com/")`：`base_url` 仅接受 ChatGPT 根地址或本地回环根地址，后者供离线页面测试。
- `await start()`：启动可见浏览器，保持 `PAUSED/config`，不会自动确认登录或账号设置。
- `await resume(*, account_settings_verified=False) -> BrowserStatus`：先核验及清理已记录残留，再核验页面就绪。人工实际关闭记忆、引用聊天历史和自定义指令后，调用方才可传 `True`；登录失效或验证码会使确认失效。状态为 STARTING、READY、PAUSED、STOPPING，暂停原因见 `pause_reason`。
- `await ask(task_id, prompt, timeout_s, *, keep=False) -> BrowserAnswer(text, conversation_id)`：开始前记录保留快照；每个任务独立页面，问题只提交一次。只有本次匹配的问题、非空回答、生成停止、完成控件和全文稳定同时满足才返回。不会在浏览器层截断答案。
- `BrowserError.category` 提供脱敏分类，`submission_unknown=True` 表示提交结果不确定，调用方不得重试该问题。取消传播 `CancelledError`，不会返回迟到答案。
- `await cleanup(task_id, keep=None) -> CleanupResult(status, task_id, conversation_id, reason)`：以原记录保留策略为准，兼容参数不能覆盖它；结果为 not_created、retained、deleted、cleanup_failed、cleanup_unknown。回答结果和清理结果分别记录。ask 尚未退出时返回 busy；先取消并等待 ask，再在 finally 中清理。
- `await close()`：停止新操作，取消自身问答、等待清理并释放自身浏览器资源；runner 不应从 ask 内部关闭整个浏览器。

`owned_sessions.json` 只保存任务编号、时间、自建会话 ID/URL、原保留策略、提交/回答/清理状态；不保存正文或凭据。通过临时文件、fsync 和原子替换写入。未知会话 ID不猜测，不遍历删除历史；损坏记录或未解决清理使恢复保持暂停。当前生产选择器仍需登录后实测，本地 fixture 成功不等于真实站点已接通。

当前 main.py runner 按 `ask(..., keep=job.config["keep_conversations"]) → send_answer → finally cleanup` 编排；清理前设置 `job.phase = JobPhase.CLEANING`，使用 `CleanupState(result.status.value)` 映射到调度器。恢复准入先确认浏览器 READY、旧任务退出，再调用调度器 resume。

必要本地回归（可见 Chrome、临时 profile、127.0.0.1 页面，无真实账号或 QQ）：

```powershell
& '..\..\..\venv\Scripts\python.exe' -m unittest discover -s tests -p 'test_*.py' -v
```

本轮结果：Python 3.10.6 / Playwright 1.58.0，94 项中 93 通过、1 项旧的可选测试跳过；Task 2 本地页面测试均执行。Ruff、语法与差异检查通过。未重复执行第二套依赖版本测试。

## Task 1 独立接口

模块仅依赖 Python 标准库，支持本机 Python 3.10.6。调用约定和错误码详见交接文档的 **Task 1 实施记录**。

- `check_access(sender_id, group_id, config) -> bool`：群/用户白名单独立开关；私聊只做用户成员检查；配置或 QQ ID 无效时拒绝。ID 使用正 ASCII 十进制字符串，`None`/空 group_id 表示私聊。
- `extract_prompt(event, max_input_chars=12000) -> str`：读取 `get_messages()` 的当前 Plain 和直接 Reply.chain 的 Plain；引用失败明确抛 `PromptError.code`，不会静默忽略。超长错误另带 `actual_chars/limit_chars` 供入口说明长度，不携带正文。精确支持 `/ask ai`，`ask ai` 只在 `is_at_or_wake_command is True` 时接受；其他宿主前缀由后续入口转换。
- `split_answer(text, total_limit=12000, node_limit=1500) -> list[str]`：先截断，再按段落/换行/硬切分段，截断提示计入预算，拼接保留最终文本。
- `TaskManager(concurrency=1, capacity=5, queue_timeout_s=600, runner=...)`：单事件循环下原子准入、FIFO、跨来源用户占位、消息去重、排队超时、取消及暂停恢复；`capacity=0` 禁止排队。构造不创建任务，先 `await start()`。
- `submit(Job(...)) -> Admission` 返回是否接纳、编号和当时排队位置（0 表示直接执行）；`wait(task_id) -> JobOutcome` 等待不可变的终态元数据，不含正文。取消等待 `wait()` 的调用者不影响任务。完成结果和已完成消息去重保留 300 秒，各最多 2048 条；执行/等待中的去重不提前过期。
- `Job` 保存来源 `SourceRoute(platform_id, message_type, target_id, send_handle=None)`、用户键 `(platform_id, sender_id)`、消息键 `(platform_id, message_type, source_id, message_id)`。准入复制请求并递归冻结配置，调用方后续修改原请求不影响已接纳任务。
- runner 覆盖执行、投递及 `finally` 清理；进入清理前设置 `job.phase = JobPhase.CLEANING`，已有投递结论写入 `job.result`。取消/暂停/关闭不会再次中断 CLEANING，也不会覆盖已确认或不确定的投递结论。执行名额与用户占位直到 runner 完全退出才释放；执行、发送和清理各自的有界超时由后续 runner 实施。
- `pause(reason)` 立即停止准入、终止等待并请求执行任务退出，不等待/取消调用它的当前 runner；当前 runner 应转入 finally 清理。`resume()` 要求旧任务已退出，浏览器 READY 核验由 Task 4 调用方负责。`close()` 等待所有清理，重复/并发调用共享关闭过程；runner 内应调用 pause，直接 close 会抛 RuntimeError；已关闭的实例不重启。

仅测试 Task 1（禁用第三方包，不打开浏览器、不联网）：

```powershell
$pluginPython = '..\..\..\venv\Scripts\python.exe'
& $pluginPython -S -m unittest discover -s tests -p 'test_scheduler.py' -v
& $pluginPython -S -m unittest discover -s tests -p 'test_messaging.py' -v
```

本次用受控协程验证 7 个不同用户并发提交时 1 执行＋5 等待＋1 拒绝，以及取消、异常、清理占位、runner 内暂停、配置快照和去重生命周期。没有进行真实 QQ、网页问答或宿主加载验收；Task 0 的真实站点阻塞仍保留。

## Task 0 历史环境与接口

- 实测：Windows、Python 3.10.6、已安装 Chrome 155.0.8059.79；venv 的 Playwright 1.58.0 和全局解释器已有的 1.60.0 均通过本地生命周期测试，独立测试进程使用 ProactorEventLoop。
- 进程查询看到本轮 `main.py` 的可执行文件为全局 `Python310/python.exe`，父进程为 cmd；没有注入探测其 `sys.prefix` 或导入路径，因此两套独立测试不能替代实际宿主环境验收。没有在宿主内部探测循环，也没有执行插件装载/重载。沙箱内循环创建挂起，沙箱外独立探针正常，暂不引入线程封装。
- `requirements.txt` 范围为 `>=1.58.0,<1.61`，兼容两个实测版本。只向 venv 新增 Playwright 1.58.0 和 pyee 13.0.1，复用已有 greenlet 3.3.0、typing-extensions 4.15.0；全局 1.60.0 未变更，没有升级宿主或其他插件。
- `ChatGPTBrowser(data_dir: Path, channel="chrome")` 提供异步 `start()`、`close()`。后续集成调用方必须传入 `StarTools.get_data_dir("astrbot_plugin_browser_chat")`；profile 为该目录下的 `browser_profile/`，只使用专用资料。
- `start()` 只启动可见浏览器，不访问站点、不代表登录或账号设置通过，状态保持 `PAUSED/config`。Task 2 新增接口见上文。
- 启动每个步骤上限 30 秒；关闭 context 和 driver 各上限 10 秒。取消驱动连接时不提前关闭尚未连通的 Playwright 管道，等待握手后释放；若清理等待也超时，保留受控启动任务并标为 `PAUSED/cleanup`，迟到驱动自行关闭，后续 `close()` 可再次收尾。错误向调用方传播，不能把清理超时当作已释放。模块导入不会安装依赖或下载浏览器。

安装依赖需使用实际宿主解释器，不运行宿主升级命令。使用系统 Chrome，无需下载 Playwright 浏览器；本次没有验证 Edge。

```powershell
$pluginPython = 'C:\Users\AceSalmon\AppData\Local\Programs\Python\Python310\python.exe'
& $pluginPython -m pip install -r requirements.txt
& $pluginPython -m unittest discover -s tests -p 'test_*.py' -v
# 显式启用可见 Chrome 本地页面测试；不访问 ChatGPT，不需要账号。
$env:BROWSER_CHAT_REAL_TEST = '1'
& $pluginPython -m unittest discover -s tests -p 'test_*.py' -v
Remove-Item Env:BROWSER_CHAT_REAL_TEST
```

Task 0 历史验证中两个环境均通过 7 项测试，其中显式启用了旧的可见 Chrome 测试。Task 2 当前默认回归也会打开临时 Chrome 访问本地页面，无需该开关；当前结果以上文为准。测试使用临时 profile，不读取日常浏览器资料；开发工具 Ruff 仅安装在忽略的 `data/tooling` 下，未列入运行依赖。

## 真实站点结果与下一阶段

Task 0 历史联调中用户报告无法通过 Cloudflare 验证。Task 2 本地回归通过后，重新用专用 Chrome 打开一次 ChatGPT：就绪检查返回 login，账号与输入控件未出现；未提交问题或创建/删除会话，浏览器已关闭并保留 profile。仍需人工登录及账号设置核验；没有绕过验证或调用网页内部接口。

真实平台仍需完成设置核验及一次自建会话的提问、最终回答读取和定向删除；Task 2 的本地页面测试不替代此项验收。Task 3/4 已完成替身及隔离装载检查，真实 QQ 发送和运行中宿主热重载尚未验收。

## License

AGPL-3.0，见 [LICENSE](LICENSE)。许可证文本沿用现有 `astrbot_plugin_astock_market` 与 `astrbot_plugin_web_check` 的同类约定。
