# AstrBot Browser Chat 完整方案与开发交接（Implementation Plan）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. 本轮只交付方案；这些执行技能在用户于新对话明确要求开发后使用。子代理按任务难度选用，不为每个小任务固定增加代理。

**Goal:** 在用户现有 AstrBot + QQ / NapCat / OneBot v11 环境中，通过专用浏览器完成 ChatGPT 网页单轮文字问答并回复用户。

**Architecture:** AstrBot 插件负责命令、权限、排队、消息发送和通知；独立的浏览器模块负责网页操作与会话清理。首版单账号、默认单执行任务，站点差异集中在浏览器模块；未来实际增加 Gemini、DeepSeek 时再扩展。

**Tech Stack:** Python、asyncio、AstrBot 插件接口、OneBot v11、Playwright async API + 专用 Chrome 持久化用户目录。Task 1 仅依赖标准库；Task 0 已安装并本地验证 Playwright，真实 ChatGPT 页面流程仍未通过。

**Spec:** 本文件第 2—8 节是完整需求和设计；第 9—10 节是实施及验收计划。无需阅读旧对话即可接手。

**日期：** 2026-10-16（Asia/Shanghai）。**状态：** Task 1—4 代码及本地/替身/隔离装载检查已完成；Task 5 原完整本地测试 134 项通过。专用 Edge 已由用户确认在 Playwright 下保留登录且可访问，账号设置已人工核验；真实问答、清理和 QQ 主流程仍待验收。以下历史记录保留原阶段结论，当前接口以最新记录为准。

### 2026-10-16 专用 Edge 最小接入

当前用户授权实施 [Edge 接入方案](2026-10-16-edge-profile-integration.md)、完成后推送一次，再按指定范围真实验收。`ChatGPTBrowser(..., *, profile_name="")` 新增可选目录名称；main.py 的 `browser_profile_name` 默认空字符串。Chrome 默认 `browser_profile`，Edge 默认 `browser_profile_msedge`；显式名称仅限 1—64 位 ASCII 字母、数字、下划线和短横线，拒绝 Windows 保留名称及越界路径。旧 Chrome 会话记录兼容；其余资料目录独立使用 `owned_sessions.<目录名>.json`，不会读取旧目录的归属记录。问答/清理/恢复结果类型和调度、投递接口不变，无新增依赖。当前部署复用插件已有的 `edge_manual_diagnostic`，不迁移或导出凭据。测试及真实验收结果由本轮最终记录补齐，登录访问通过不能替代完整主流程。

### 2026-10-15 Task 4 实施记录（当前入口与验证边界）

按本轮要求先推送 Task 3：`36d087e` 已推送 origin/main。随后新增 main.py、metadata.yaml、_conf_schema.json、tests/test_plugin.py；消息模块仅补 dependency 安全故障码及对应测试，浏览器/调度代码与测试未变。无新增依赖，不安装宿主缺失包，不自动提交或推送 Task 4。

BrowserChatPlugin 继承 Star 注册，构造不建任务、不安装依赖；initialize 创建 TaskManager 并保持暂停，使用 StarTools.get_data_dir 的专用浏览器，未核验账号设置不会 READY。配置无效时报告字段名并暂停 config，Playwright 缺失时暂停 dependency。配置 schema 的 16 个默认字段与 validate_config 一致，拒绝布尔冒充数字、NaN/无穷/超大超时整数、非法 QQ ID及模板；任务使用准入配置快照，重载新实例取新配置。

- 单个锚定 RegexFilter 接受 `/ask ai`、`/ask status`、`/ask resume`、`/ask cancel`；不叠加 command 过滤器。剥离宿主前缀后的 `ask …` 仅在 is_at_or_wake_command=True 时执行。直接引用仅提取 Plain，已确认的宿主前缀通过原 Plain 链规范化，不以合并 message_str 充当问题正文。
- 认领后、任何 await 前 stop_event；普通 async handler 直接 await event.send 回执，成功 None 不视为失败，不 yield 回执。仅 OneBot 来源可操作，防止其他平台伪造相同 QQ ID获得插件管理员权限。status/resume 使用宿主 is_admin 或 admin_ids，独立于使用白名单；cancel 只针对当前来源平台与调用者用户键，无任意任务/用户参数。
- `/ask resume` 不自动确认设置；`/ask resume 已核验` 仅供管理员在人工登录并关闭记忆/引用历史/自定义指令后使用。生命周期锁和 closing 标记串行化恢复与卸载；旧 runner 尚在清理时不探测/恢复浏览器，不开放准入。
- runner 按生成、发送、finally 清理编排。生成用 asyncio.wait 的完成集合明确判定截止；超时或取消先取消并等待生成子任务退出，即使底层吞取消返回答案也不发送。180 秒只限生成，发送共享独立 30 秒预算，清理沿浏览器自身 20 秒预算，不叠加可能提前放槽的外层清理超时。
- 发送前再次检查取消；confirmed 记 SUCCESS，unknown/不确定 partial 记 DELIVERY_UNKNOWN；DeliveryCancelled.result 先记录再传播取消。CLEANING 不再注入第二次取消，名额直到 runner 完全退出才释放；已确认或不确定的投递不被清理失败/后续取消覆盖。取消回执明确说明已提交或结果未知的消息不会撤回。
- 每个接纳任务有一个不保存正文的终态 watcher，覆盖未进入 runner 的排队超时/暂停/取消。用户失败提示只发送一次；超时、浏览器/投递/清理故障交给按来源绑定的 notifier。清理失败独立记录、暂停准入，成功答案不重发或改判失败。失败投递日志保留确认段数及 uncertain，无正文。
- 来源事件通过 OneBotBinding.from_event 绑定；初始化时对已启用 OneBot 实例使用公开 get_login_info 读操作确定账号（每实例最多 5 秒），不使用随机 client_self_id 或私有连接表。无法识别时只记录日志，待合法来源事件注册绑定；同一平台 ID出现不同机器人账号要求重载，拒绝错路由。每来源持有独立配置快照 notifier，无需修改 Task 3 的接口。
- async terminate 直接定义在插件类中，不定义 __del__。关闭准入后等待调度器完整清理、关闭专用 browser、等待终态提示，再取消并等待通知计时器/发送。并发 terminate 共享关闭任务，初始化/恢复期间卸载不会重新开放。

**本轮验证：** venv Python 3.10.6 下必要回归共 87 项（messaging 50、scheduler 15、plugin 22）全部通过，4.625 秒；不重跑未改动的浏览器套件。关键 RED→GREEN 包括模块缺失、状态字段接缝、吞取消的迟到回答、超大超时整数及依赖故障分类；单次只读复核未发现确定 P1/P2。Ruff/语法/差异检查见最终交付记录。

**真实源码隔离装载：** venv 直接导入宿主 API因缺 loguru 受阻，未补装或升级；已安装的全局 Python 3.10.6 在独立 ASTRBOT_ROOT 下成功导入真实 API及正式插件路径，用真实 PluginManager schema解析、AstrBotConfig、metadata、Star/handler 注册进行检查。真实 WakingCheckStage→StarRequestSubStage→ProcessStage 处理 5 个命令（含 #ask ai），2 次问题交给浏览器替身，默认 LLM调用为 0；回执替身故意不置 _has_send_oper，证明 stop_event 可阻止默认 LLM。会话插件启用策略固定为允许，浏览器/OneBot/LLM替身，不是真实 QQ 验收。

实际 PluginManager._terminate_plugin 成功调用插件 async terminate，再构造并卸载新实例验证生命周期；仅在隔离子进程中，不是对运行宿主调用 reload_plugin。宿主声明 Python >=3.12，以上源码检查不能推出官方支持 3.10 或运行宿主的 sys.prefix。没有访问真实浏览器或发送真实 QQ，也没有重启、重载运行宿主。

后续若进行真实插件热重载，影响为：仅本插件停止准入、取消旧任务并等待清理，可能给已有任务发送失败提示/管理员通知，关闭本插件专用浏览器再创建新实例，人工设置确认需重新完成；已提交 QQ 消息不可撤回。不得重启整个宿主。此运行中验收、人工登录/真实 DOM问答删除及获准 QQ对象的收发仍是未解决验收项，Task 5 继续处理。

### 2026-10-15 Task 3 实施记录（当前接口与授权）

按用户要求先提交并推送一次 Task 2：`67181db` 已推送至 origin/main，仓库可见性未变。随后只修改 messaging.py、对应单元测试及文档；不再次提交或推送 Task 3，不发真实 QQ，不修改 browser_chat.py、scheduler.py 或其测试行为。没有新增依赖。

当前接口（暂无 main.py 调用方需要迁移）：

- `OneBotBinding(platform_id, self_id, client, sender_name="BrowserChat")`，推荐 `OneBotBinding.from_event(event)` 从公开 getter 和 event.bot 构造，存入 SourceRoute.send_handle。严格核对来源实例、有效目标及 group/private；每次 call_action 注入原 self_id。不会保留 AstrBot event。
- `await send_answer(route, parts, *, timeout_s=30) -> DeliveryResult`。群/私聊分别调用 send_group_forward_msg/send_private_forward_msg；节点使用机器人 uin、署名及纯 text 段。仅明确失败后普通消息有限降级一次；共享总超时，不重试已确认段或未知发送。
- `DeliveryResult(status, confirmed_parts, total_parts, used_fallback, uncertain, reason)` 为不可变元数据，不含正文；status 为 confirmed/partial/unknown/failed。partial 区分已确认段数及失败段不确定性。confirmed 仅为 API 接受，不能宣称用户已读或客户端最终展示。
- `DeliveryCancelled(Exception).result` 保留取消进度。Python 3.10 跨 Task 会丢失 CancelledError 子类自定义字段，故这里用普通异常传输；Task 4 必须捕获、记录 result 后重新抛 asyncio.CancelledError。发送前取消不提交；在途取消标 uncertain，停止未提交段。
- `Fault(task_id, source, category, reason="", browser_account_scope="default", occurred_at=UTC now)` 只传元数据，source 为 SourceRoute 或 None（启动故障）。reason 为控制码，任意原始错误丢弃；模板字段只从固定码映射和受限 ID构造，日志不输出原异常、Cookie、Token、路径、问题或答案。摘要保留首条发生时间。
- `validate_template(template) -> str` 拒绝未知字段、属性/下标、转换及非空格式说明；仅允许 task_id/reason/category/source/time/count。`render_failure(fault, *, template=None, admin=False, count=1) -> str` 在模板无效时记录配置警告、使用安全默认文案；替换值不允许注入换行/CQ 段。
- `await send_failure(route, fault, *, template=None, timeout_s=30) -> DeliveryResult` 给来源用户发一次普通提示；用户模板取任务准入配置快照，不在失败时自动调用通知。
- `FailureNotifier(bindings, admin_ids, *, admin_failure_template=...)` 复制配置、去重管理员；`await notifier.notify_failure(platform_id, fault)` 使用精确来源 binding。按 `(platform_id, browser_account_scope, category)` 合并 300 秒，首发立即，后续累计，窗口末有新增才补总次数摘要。通知异常/False/未知只安全记录，不递归、不重试；空列表警告，错平台不兜底。
- `await notifier.close()` 取消并等待自己所有计时器和发送；通知层恢复正常 CancelledError 语义，不发卸载汇总。构造可注入 window_sleep，仅用于受控窗口测试，默认 asyncio.sleep(300)，不是面板配置。

当前安装源码核验：AiocqhttpMessageEvent.send/send_message 丢弃 OneBot 数据、成功返回 None；Context.send_message bool 仅代表找到平台。为避免混淆，本模块使用原始 CQHttp.call_action。已安装 aiocqhttp 的 `_handle_api_result` 在明确失败时抛 ActionFailed，成功时解包 data。ActionFailed、ApiNotAvailable、显式 failed 响应才按拒绝；原始 None/False/连接异常/超时/async 或未知响应按 unknown，不全文降级。没有调用 event.send 或 Context.send_message 后再按真假重发的路径。

Task 4 接入：生成成功后用 split_answer 再 send_answer；confirmed 映射 SUCCESS；unknown 或 partial 且 uncertain 映射 DELIVERY_UNKNOWN；明确 failed/partial 按失败处理并保留 confirmed_parts。取消先记录 DeliveryCancelled.result，传播取消后 finally 清理；不可根据取消承诺撤回已提交消息。管理员由 notifier.notify_failure 处理，权限/队满/空输入只提示用户；启动故障由入口显式枚举已启用 OneBot 来源。terminate 调用 notifier.close，不留后台通知任务。

单次只读复核发现通知取消时普通 DeliveryCancelled 外泄，已用阻塞发送的 cancel/close 测试复现并修正：通知层安全记录后恢复 CancelledError。其余未发现确定的 P1/P2。按额度要求只跑必要消息、调度回归和静态检查，不重跑浏览器或第二套环境。

**最终验证：** venv Python 3.10.6 下 messaging 49 项全部通过（1.466 秒，包括已安装 aiocqhttp 的实际 ActionFailed 和 data 解包语义，未跳过）；scheduler 在 -S 标准库环境下 15 项通过（1.378 秒），合计本轮必要回归 64 项通过。Ruff check/format、py_compile、git diff --check 通过；浏览器、调度器及其测试相对 67181db 无差异。网络与 QQ 发送均由替身隔离；没有真实 QQ、宿主加载或浏览器回归验收。Task 3 当前修改未提交/推送，后续 Task 4 需按以上契约接入 main.py。

### 2026-10-15 Task 2 实施记录（当前接口与授权）

按用户要求先推送一次：`35380c4` 已推送到 `origin/main`，内容为 Task 0/1 基线。随后仅修改 `browser_chat.py`、浏览器测试、本地 fixture 和文档；Task 2 保留为未提交修改，不再次自动推送。`scheduler.py`、`messaging.py` 及其测试行为保持不变，不发送 QQ，不实现宿主入口。

当前 `ChatGPTBrowser` 不依赖 AstrBot 事件，接口为：

- `ChatGPTBrowser(data_dir: Path, channel="chrome", *, base_url="https://chatgpt.com/")`：生产地址只允许 ChatGPT 根地址，回环根地址用于本地测试；专用 profile 保持不变。
- `start()` 启动后保持 `PAUSED/config`。`resume(*, account_settings_verified=False) -> BrowserStatus` 先处理已知残留，再核验页面；人工实际关闭记忆、引用聊天历史和个人自定义指令后，调用方才可传 True。登录失效或验证码使确认失效；不能把此参数自动设为 True。
- `ask(task_id, prompt, timeout_s, *, keep=False) -> BrowserAnswer`：新增 keep 关键字用于提交前持久化原保留策略。返回不可变 `BrowserAnswer(text, conversation_id)`；ID可能未知。异常使用 `BrowserError.category/submission_unknown`，取消传播 `CancelledError`。
- `cleanup(task_id, keep=None) -> CleanupResult`：返回不可变 status、task_id、conversation_id、reason；status 为 not_created/retained/deleted/cleanup_failed/cleanup_unknown。keep 仅作兼容参数，不能覆盖记录中的策略。ask 活跃时返回 busy，调用方应先取消并等待 ask 退出，再在 finally 中清理。
- `close()` 停止新操作、取消自身问答、等待清理并关闭自身 context/driver；晚到的页面创建仍有归属和收尾任务。resume 在操作未退出、清理残留未解决或写入未成功时不会 READY。

完成判定同时检查新出现且与本次问题匹配的 user 消息、本次非空 assistant、停止生成、完成控件和全文稳定窗口；不以旧答案、空白或长稳定前缀冒充完成。问题只点击提交一次，提交不确定时不重试；URL变化不能单独建立会话归属。超时及取消不会返回可继续投递的迟到答案。浏览器返回完整答案，截断分段仍由 Task 1 的 split_answer 负责。

`owned_sessions.json` v1 保存 task_id、UTC创建时间、conversation_id/url、keep 快照、submission、answer_result 和 cleanup，回答与清理独立记录。原子写入使用同目录临时文件、flush/fsync、os.replace；不写正文或凭据。加载校验字段、类型、同源精确地址及“有 ID必须已确认提交”关系，损坏文件保留并暂停。重启仅处理这些记录，不枚举删除账号历史；未知 ID的默认删除返回 cleanup_unknown。删除确认不明确时不会记为 deleted。

后续 Task 4 的 runner 使用 `ask(..., keep=job.config["keep_conversations"]) → send_answer → finally cleanup`，清理前设置 `job.phase = JobPhase.CLEANING`，将 `CleanupState(result.status.value)` 写入任务。错误交给 Task 3 的通知层，禁止因 submission_unknown 重复提问。恢复准入先确认浏览器 READY及旧任务退出，再调用 TaskManager.resume；当前无 main.py 调用方需要迁移。

本轮采用本地 HTTP 可控 DOM 页面及临时 Chrome profile，覆盖逐步生成、长停顿、空回答、稳定长前缀、代码链接、旧回答、登录失效、验证码、地区错误、限流、页面结构变化、提交不确定、不匹配问题、超时、取消、并发标签页、定向删除、保留快照、重启残留、坏记录和原子写入失败。生产 DOM 选择器仍是候选，必须登录后单独验证，不能由 fixture 通过推定可用。用户要求节省额度后，仅保留当前必要回归，不再进行第二套 Playwright 版本复测或额外审查。

**最终本地验证：** venv Python 3.10.6 / Playwright 1.58.0 / 系统 Chrome，`unittest discover` 共 94 项，93 通过、1 跳过，耗时 162.406 秒；跳过的是 Task 0 旧的可选可见 Chrome 测试，Task 2 的本地页面测试均已执行。Ruff check/format、py_compile 和 git diff --check 通过；调度与消息源文件及其测试相对推送基线无差异。未运行宿主加载、QQ 或第二套版本复测。

**真实页面核验：** 本地全部通过后，用插件专用 profile 打开一次 ChatGPT 根页面，3 秒后的就绪检查返回 `login`，账号控件和输入控件均未出现，页面标题未命中 Cloudflare 挑战特征。没有据此宣称登录成功或地区问题已解决。未提交问题，未创建或删除会话；专用浏览器已正常关闭，profile 保留。仍需人工登录、账号设置核验，以及候选 DOM 选择器/一次问答和定向删除的真实验收。这是未解决验收项，不以本地测试替代。

### 2026-10-15 Task 1 实施记录（当前接口与授权）

用户本轮明确要求独立执行 Task 1；Task 0 真实登录受阻不妨碍本轮纯逻辑测试。不实现网页生成、QQ 发送、通知或插件入口，不自动提交/推送/发布。新增 `scheduler.py`、`messaging.py`、`tests/test_scheduler.py`、`tests/test_messaging.py`，保留 Task 0 文件及已有修改，无新增运行依赖。

**调度接口：** 保留第 7 节的 `TaskManager(concurrency=1, capacity=5, queue_timeout_s=600, runner=...)` 和 `start/submit/cancel/pause/resume/snapshot/close`，补充 `await wait(task_id) -> JobOutcome` 供 Task 4 取得等待超时、取消、暂停及 runner 结束的单一终态，避免等待任务未执行 runner 而无法反馈失败。构造不启动任务；`close()` 后实例不再开放，创建新实例代替重启。

| 数据/方法 | 本次稳定契约 |
| --- | --- |
| `Job` | 必填 `task_id, dedup_key, user_key, prompt, route`，`config` 默认空 mapping；阶段、结果、清理及单调时间由准入与 runner 管理。传入请求不作为运行对象，`submit()` 复制并重置生命周期字段，配置递归冻结为 mapping/tuple。runner 只修改阶段/结果/清理及诊断类别，不修改身份、路由或准入配置。 |
| `SourceRoute` | 不可变 `platform_id, message_type, target_id, send_handle=None`；目标字段须与消息去重键前三项一致，用户键的平台也须匹配。send_handle 保留必要运行句柄，不放整个事件。 |
| `Admission` | `accepted, task_id, queue_position, reason`；位置 0 为直接执行，1 起为当时等待位置。拒绝原因：`not_started/paused/closed/duplicate_message/duplicate_task_id/user_busy/queue_full`；非法 Job/配置抛 ValueError，拒绝不占用户或消息位置。 |
| `QueueStatus` | 不可变 `state, running, queued, pause_reason`，不含任务或正文；running 包括投递/清理。状态为 `NEW/RUNNING/PAUSED/CLOSING/CLOSED`（枚举值小写）。 |
| `JobOutcome` | 不可变 `task_id, result, cleanup, admitted_at, started_at, finished_at, reason, error_type, phase=DONE`，不保存正文、配置或异常对象。完成结果缓存 300 秒、最多 2048 条，过期/未知编号的 wait 抛 KeyError；取消 wait 的调用者不取消底层任务。 |
| `cancel(user_key)` | 返回是否找到未完成任务；等待任务立即终止，执行任务请求取消并等待完整退出，当前 runner 自调用仅设置标记。直到清理结束才释放名额。 |
| `pause(reason)` | 原子关准入、终止等待任务（FAILED/paused）、请求执行任务退出；不等待/取消当前 runner，避免自锁，当前 runner 应直接转入 finally 清理。 |
| `resume()` | PAUSED 且执行任务全部清理退出后才开放；未就绪抛 RuntimeError。浏览器检查 READY 仍由 Task 4 协调，本模块不虚构站点状态。 |
| `close()` | 停止准入、取消等待/执行、等待退出；重复和并发调用共享过程，取消某个 close 调用者不取消实际关闭。runner 直接调用 close 抛 RuntimeError，使用 pause 代替。 |

执行中的消息去重保持到终态；已完成消息另保留 300 秒、最多 2048 条（有界短期去重）。同文不同消息不去重。等待期限用单调时钟，定时移出，不依赖前一任务结束。`queue_capacity=0` 严格不排队。任务状态枚举包括第 5.3 节的所有阶段、结果与清理状态；等待超时为 `TIMED_OUT/queue_timeout`，取消为 `CANCELLED`，未处理 runner 异常为 `FAILED/runner_error`，runner TimeoutError 为 `TIMED_OUT/runner_timeout`，仅保存异常类型。

runner 包含完整执行、投递和有界 finally 清理；进入清理前显式写 `phase=CLEANING`，调度器此时只标记取消，不再打断清理。已确定的 `SUCCESS/DELIVERY_UNKNOWN` 等结果不会被后续取消覆盖；尚无结果时由调度器归结。Task 4 负责分别落实 180/30/20 秒预算和发送前的 `cancel_requested` 检查，不将 180 秒施加到整个 runner。

**消息接口：** `check_access(sender_id, group_id, config)`、`extract_prompt(event, max_input_chars=12000)`、`split_answer(text, total_limit=12000, node_limit=1500)` 与草案一致。白名单 QQ ID 使用非空正 ASCII 十进制字符串，开关须为 bool，列表支持 list/冻结后的 tuple；配置/ID 无效时 check_access 返回 False。私聊用 None/空 group_id，忽略群成员限制，但所有配置字段仍须合法。

`extract_prompt` 只读实际宿主的公开 `get_messages()`、`is_at_or_wake_command` 及组件 `type/text/chain`。精确接受 `/ask ai`，仅明确唤醒时接受 `ask ai`；自定义前缀转换留 Task 4。`PromptError(ValueError).code` 为 `invalid_event/invalid_command/reference_unresolved/reference_no_text/empty_prompt/input_too_long`；超长错误另有 `actual_chars/limit_chars`，其余错误这两项为 None，均不携带正文。id-only Reply 的默认空链无成功元数据视为 unresolved；None 链同样拒绝；已解析但无 Plain 文字视为 no_text，不回退使用 message_str 或嵌套引用。合并标签计入 12,000 输入预算，CQ 标记不作为问题。split_answer 保留代码与 Unicode 文本，截断标记 `\n\n[内容已截断]` 计入总预算；非法长度配置抛 ValueError。

**当前验证：** 15 项受控调度测试及 32 项消息测试；`Python 3.10.6 -S` 禁用 site-packages 验证无 AstrBot/浏览器依赖。覆盖 7 人原子准入的 1+5+1、FIFO、0 队列、跨群/私聊用户占位、主动等待超时、消息去重/到期/有界历史、取消/异常后释放、自然清理期间取消、runner 内暂停、自关闭保护、暂停恢复、共享关闭与取消等待者、配置快照、输入输出边界及不带正文的超长计数。独立只读复核未发现已确认 P1/P2；最终回归结果见交付。这些不是宿主装载、网页或真实 QQ 验收。

**最终检查：** venv Python 3.10.6 在 `-S` 下 Task 1 **47/47 通过**；现有全局解释器默认全套回归 **53 通过、1 跳过**（总计 54 项，可见 Chrome 测试未启用）。本次四个新增 Python 文件 Ruff 格式/静态检查、compileall 和 `git diff --check` 通过。未打开浏览器、访问真实站点或发送 QQ；既有 Task 0 默认回归仅使用驱动连接测试，不把该结果当作网页登录通过。

交接 Task 1 原计划中的“错误模板”随本轮用户限定范围留在 Task 3；其他 Task 1 行为已覆盖。Task 0 历史记录继续保留，不能由本轮纯逻辑通过推定真实站点可用。

### 2026-10-15 Task 0 实施记录（历史阶段）

本次用户明确限定 **仅 Task 0**，并另行授权人工登录专用 profile、核验账号设置及一次真实 ChatGPT 提问/删除测试。没有真实 QQ 测试对象，不进行 QQ 收发；不提交、推送、发布或变更仓库可见性。Task 1—5 未实施。

- 新增 `browser_chat.py` 的 `BrowserStatus`、`ChatGPTBrowser.start/close`，以及 `requirements.txt`、`tests/test_browser_chat.py`。构造为 `ChatGPTBrowser(data_dir: Path, channel="chrome")`，后续调用方通过 `StarTools.get_data_dir("astrbot_plugin_browser_chat")` 传入专用数据目录。`context` 当前仅供 Task 0 探针使用；Task 2 的 `ask/cleanup/resume` 尚未实现，不把临时人工探针当成站点适配器。
- `start()` 不访问网站，成功后仍为 `PAUSED/config`；登录、设置及网页状态尚未由模块自动核验，不能使用该状态接收任务。每次驱动/Chrome 启动上限 30 秒，context/driver 关闭各 10 秒。驱动连接中取消会等待握手后关闭，不提前关闭尚未连通的管道；若清理等待也超时，保留受控启动任务并设置 `PAUSED/cleanup`，迟到驱动自行关闭，后续 `close()` 可再次收尾，不能虚报清理完成。仅关闭本模块的持久化上下文，不访问日常 profile。
- 进程查询补充历史环境证据：本轮 `main.py` 的可执行文件路径为全局 `C:\Users\AceSalmon\AppData\Local\Programs\Python\Python310\python.exe`，父进程为 cmd；没有注入探测其 `sys.prefix` 或实际导入路径，不能仅凭进程路径排除 Windows venv 重定向的影响。该全局环境独立检查为 Python 3.10.6，已有 Playwright 1.60.0、pyee 13.0.0、greenlet 3.3.0。本轮未改变全局依赖。
- 目录内 venv 同为 Python 3.10.6，本轮仅新增 Playwright 1.58.0、pyee 13.0.1，复用既有依赖。两个环境均通过本地测试，因此依赖范围为 `playwright>=1.58.0,<1.61`，不要求降级现有 1.60.0。使用已安装 Chrome，不下载浏览器，不升级宿主或其他插件。
- **模拟测试：** 初始化取消、启动取消与重启、关闭异常释放驱动、重复启停及不误报 READY、超时后保留并关闭迟到驱动。**真实驱动回归：** 延迟真实 Playwright 管道连接后取消，确认取消异常不被清理覆盖且没有遗留 Connection.run。**本地真实浏览器测试：** Chrome 155.0.8059.79 可见窗口、本地 HTML 文本读取、取消本地会话后关闭页面、重新启动/关闭。两个解释器最终均通过 7 项测试（含 1 项显式启用的真实本地浏览器测试）。
- 独立测试进程实测 `ProactorEventLoop`；沙箱内创建循环挂起，沙箱外正常，不能把沙箱现象解释为宿主 Selector 冲突。未在运行中的宿主内部检测循环，未执行宿主装载/重载，不提前引入线程封装。
- Ruff 0.14.0 安装在忽略目录 `data/tooling`，不进入宿主环境及运行依赖；代码格式和静态检查通过。原有 `.gitignore` 等修改保留，运行 profile 位于宿主 `data/plugin_data/astrbot_plugin_browser_chat/browser_profile`；临时探针及证据仅在忽略目录。
- **真实 ChatGPT：Cloudflare 验证阻塞。** 已打开专用 Chrome 并确认登录入口可见，用户报告无法通过 CF 验证，随即停止联调并关闭自建窗口，保留专用 profile。未完成登录及账号设置核验，未提交问题、验证最终回答或创建/删除测试会话；不尝试绕过验证或反复重试，Task 0 仍未全部完成。
- 独立只读代码复核发现真实 manager 在连接未完成时取消会掩盖取消异常并遗留后台连接；主代理已复现，补充失败回归后修复，两个环境最终 7/7 通过。修复只使用 Playwright 公共生命周期接口；测试为复现边界替换了传输连接方法。语法、Ruff、差异空白检查通过。

后续 Task 2 复用启动/关闭和专用数据目录参数，补齐网页就绪判定、任务归属记录、生成及定向清理；不得据本地页面通过宣称真实站点已接通。Task 4 再提供插件入口并实测运行中宿主的生命周期。

## 1. 接手入口与当前状态

| 项目 | 已核实的信息 |
| --- | --- |
| 插件目录 | `E:\AstrBotLauncher-0.2.0\AstrBotLauncher-0.2.0\AstrBot\data\plugins\astrbot_plugin_browser_chat` |
| 宿主目录 | `E:\AstrBotLauncher-0.2.0\AstrBotLauncher-0.2.0\AstrBot` |
| GitHub | `https://github.com/AceSalm0n/astrbot_plugin_browser_chat`；上轮浏览器已确认 Private，继续保持私有 |
| Git 基线 | 本轮开始时 `main` 和本地 `origin/main` 均为 `de5a84a`；未重新 fetch，不能据此声称再次核对了服务器最新状态 |
| 历史保留 | `local-init-20261015` 保存最初本地初始化提交；不删除或强推覆盖 |
| 原有文件 | `AGENTS.md`、`README.md`、`.gitignore`、`LICENSE`；没有功能入口、配置 schema、依赖或测试代码 |
| 开源协议 | AGPL-3.0；参考既有行情与网站检查插件，远端最终使用 GitHub 标准许可证文本 |
| 宿主版本 | 当前源码 `astrbot/__init__.py` 为 4.28.0 |
| 解释器差异 | `AstrBot/venv/Scripts/python.exe` 实测 3.10.6，而 `pyproject.toml` 声明 Python >=3.12；运行中进程使用哪个解释器需开发时再次核实 |
| 浏览器依赖 | 本机存在 Chrome/Edge；该 venv 未安装 Playwright；没有进行真实网页提问、登录或 QQ 验收 |

先读插件 `AGENTS.md` 及适用的宿主 `AGENTS.md`，再读本文件。用户已授权主代理根据难度选择子代理、模型与推理强度，最终整合和证据核验由主代理负责。仅使用调度工具实际可用的型号和档位。

本轮文档改动不等于获准安装、测试真实账号、提交或推送；新对话按用户实际下达的开发范围推进。历史的“等待 Astra 复核”是阶段说明，不能覆盖用户后续明确的实施指令。

## 2. 复查结论与前提更正

### 2.1 技术路线与网站规则分开判断

用户明确选择真实浏览器操作，不使用反向代理或直接调用网页内部接口。首版保持此路线，不擅自改为 API 方案。

但“网页无限对话”“操作浏览器便一定符合规则”不是已成立事实。2026-10-15 查询的 OpenAI 使用条款限制自动或程序化提取输出，也限制向他人提供账号使用及绕过限流。当前自动读取回答、向多人提供服务的需求与通用条款存在冲突，不能将本方案描述为已获得许可或合规保证。适用账号协议、地区和额外授权应由部署者核实。[OpenAI Terms of Use](https://openai.com/policies/terms-of-use/)

这是对最初动机的事实更正，不是已发生封号或额度耗尽的结论。插件应正确识别限流、验证码及登录失效并停止任务，不设计绕过机制。

### 2.2 需求与本轮设计建议的区别

下文 **已确定** 表示用户在旧对话明确选择或接受的要求；**复查建议** 表示为使实现完整而新增的默认设计，不应伪称用户逐项批准。新对话可采用这些建议开始实现；若实际页面或宿主证据要求改变用户可见行为，先说明具体差异。

本轮主要补充：命令去重、引用纯文字提取、有限输入、队列原子准入、取消后清理、发送结果不确定性、故障去重粒度、恢复入口和重启残留处理。

## 3. 已确定的产品需求

| 范围 | 首版要求 |
| --- | --- |
| 平台与站点 | QQ / NapCat / OneBot v11；ChatGPT 首发，Gemini 然后 DeepSeek 为后续候选 |
| 输入 | `/ask ai 问题`；只支持文字，不处理图片、附件、语音、合并转发或嵌套引用 |
| 引用 | 附带直接引用那条消息中的文字；有额外问题时两者组合，仅有引用时把引用文字当问题；引用无法读取则提示失败 |
| 会话 | 每个请求新建会话，不提供连续追问；专用浏览器、手动登录、保留登录状态 |
| 账号设置 | 关闭跨会话记忆及个人自定义指令；须核验账号设置，新建页面不自动保证隔离 |
| 权限 | 默认所有人可用；群白名单、用户白名单独立启用；群聊两项都开启时两项都须满足；私聊只检查用户限制 |
| 排队 | 默认执行 1 个、等待 5 个，均可配置；每位用户最多一个未完成任务；满额拒绝 |
| 超时 | 默认执行 180 秒、等待 600 秒，可配置；告知失败，不自动重新提交问题 |
| 输出 | 回答完成后以一条合并转发发送；内容过长分节点；默认总上限 12,000 字符、节点上限 1,500 字符，可配置 |
| 输出降级 | 合并转发明确失败后尝试普通分段消息；保留代码文本和可提取的引用链接，不做图片渲染 |
| 会话保留 | 面板可选，默认不保留；仅删除插件创建的会话 |
| 管理员通知 | 执行/排队超时、网页失败、登录失效、验证码、最终发送失败及清理失败通知管理员；管理员列表、用户失败提示、管理员通知模板可配置 |
| 通知规则 | 无权限、队满、空问题只回复用户；相同故障 5 分钟内合并；默认不包含问题及回答正文 |

白名单按 QQ 用户/群的字符串 ID 匹配。多 OneBot 实例的任务去重、占位和发送路由另外携带来源平台 ID，不要求在白名单中填写复合 ID。“默认所有人可用”指插件授权规则，不表示未登录、配置损坏或浏览器故障时仍接单。

## 4. 输入、输出与消息处理

### 4.1 命令及引用

1. 仅处理精确的 `/ask ai` 命令前缀和后续空白/正文，不能误匹配 `/ask aibot`。首版不引入网页地址参数或自动执行回答中的指令。
2. 当前宿主群聊全局前缀为 `#`。源码链路允许单独 RegexFilter 识别 `/ask ai` 并唤醒 handler；不能再叠加要求唤醒标记的 command/command_group 装饰器。首版保证 `/ask ai`；宿主剥离前缀后的 `ask ai` 仅在事件已被前缀或 @ 唤醒时接受，避免裸文本误触发。若兼容 `#ask ai`，入口汇聚到同一准入逻辑。仍受宿主自身白名单、限流和会话启用状态约束，真实 QQ 尚未验收。
3. 取得当前消息的 `Plain` 内容并移除命令；引用部分仅取直接 `Reply.chain` 中的 `Plain`。保留文字内部换行，不把图片说明、CQ 代码或 JSON 序列化结果当问题。
4. 引用包含图片和文字时取其中的文字；区分“没有 Reply”“有 Reply 但未解析”“解析成功但无文字”。宿主 get_msg 失败后仍可能留下只有 id 的 Reply，不能只检查 Reply 存在。只有图片或引用读取失败时明确拒绝；即使还有额外问题，也不静默丢弃引用继续执行。
5. 同时有两段文字时，按 `引用消息：\n...\n\n本次问题：\n...` 组合；只有一段则直接提交该段。不要附带用户昵称、QQ 号、群号或旧聊天历史。
6. **复查建议：** 合并后的输入默认最多 12,000 字符，面板可改；超过后拒绝并说明长度，不静默截断用户问题。入队前完成权限、引用及长度校验。
7. 消息认领后阻止同事件继续触发宿主默认 LLM 回复；如何设置 `stop_event()` 以当前事件流水线为准。用 `(platform_id, message_type, source_id, message_id)` 做短期去重，不能仅靠问题文本。

### 4.2 输出格式与长度

- 长度按 Python Unicode 字符计数，不是 token 数或字节数；不要承诺等同视觉字形数量。
- 按总上限截断，然后按节点上限拆分，优先段落、换行，最后硬切。截断标记 `\n\n[内容已截断]` 计入总上限；要求配置上限足以容纳标记。
- 上限作用于最终答案文本，包括代码与链接。节点标题、任务编号只作短标签，不能借元数据继续塞入超限答案。
- 转发节点署名为机器人/ChatGPT，不冒充原引用者。群聊和私聊分别调用对应的 OneBot 转发接口。
- **复查建议：** 只有明确被平台拒绝或确定不支持转发时才降级。网络超时、连接中断发生在提交后，记为“发送结果未确认”，不整段重发；否则可能出现双份答案。
- 普通分段发送逐段确认；第 k 段失败后不从第 1 段重新发送。停止剩余发送，记录已确认发送的段数，通知用户和管理员。
- 页面仍在生成时，即使前 12,000 字符不再变化，也不能据此判断完成；必须观察生成状态及完整响应的变化信号。

## 5. 浏览器、并发与任务生命周期

### 5.1 浏览器运行方式

**复查建议：** 首版使用 Playwright async API 的可见 Chrome 持久化上下文，专用 profile 放在 AstrBot 插件数据目录，不放 Git 仓库。人工初次登录，不读取日常浏览器凭据、不导出 Cookie。独立 profile 不等同独立账号；关闭记忆和自定义指令会影响所登录账号，优先使用专门用于机器人的账号。

一个 profile 由一个插件实例持有；每任务独立标签页与新网页会话。并发上限是整个插件共享的上限，不能每个群各自获得一套 1+5。更高并发需要多标签页真实验证，不承诺服务端接受同账号并行生成。[Playwright 持久化上下文](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context)

首版使用网页当前可用的默认模型；不模拟模型配额切换，不支持深度研究、代理模式、文件上传或后台长任务。未来提供模型选择时另行设计。联网引用属于普通答案文本提取，不承诺特殊模式产物。

生命周期：`initialize()` 检查依赖/配置并准备资源；缺依赖时给出安装说明并保持暂停，插件导入不得自动执行 pip 或下载浏览器。登录失效时保留专用浏览器供人工处理。`terminate()` 停止准入、取消等待和执行任务、等待清理，再关闭插件拥有的上下文。只实现 async terminate，不定义 `__del__`；当前宿主的旧析构分支可能因此跳过 terminate。

**Windows 验证：** 当前源码入口使用 `asyncio.run()`，尚未证明实际运行循环类型。先在宿主所用解释器中验证浏览器启动、取消和关闭。Windows 的 Selector 循环不支持子进程；如确有冲突，采用插件专属线程中的 Proactor 循环封装浏览器操作，不修改宿主全局循环策略。没有失败证据时不提前增加线程或独立进程架构。[Python 平台说明](https://docs.python.org/3.10/library/asyncio-platforms.html)

### 5.2 网页操作约定

`检查登录及账号设置 → 新建独立会话 → 确认输入框 → 填入一次 → 提交一次 → 确认产生本次回复 → 等待完成 → 提取最终回答`

- 选择器基于当时真实 DOM、语义标签和稳定属性确定，集中放在浏览器模块；本交接不预填未经验证的选择器。
- 完成判定同时考虑本次 assistant 消息存在、生成停止状态/完成操作控件，以及内容短暂稳定。建议稳定窗口 2 秒、轮询约 0.5 秒，全部受总执行截止时间约束。
- 不能把空答案、用户提问、思考过程、上一轮内容、登录提示或错误横幅当答案。
- 限流、挑战页、登录失败和页面结构不匹配映射为不同错误类别。重试仅限确认尚未提交问题的读取/定位步骤；提交状态不明时不再次点击发送。
- 只读取可见回答及其链接，不拦截内部接口或注入代码访问隐藏应用状态；页面文本不能驱动任意系统操作。

### 5.3 队列与状态

推荐任务阶段：`QUEUED → RUNNING → DELIVERING → CLEANING → DONE`。结果字段独立记录 `SUCCESS / FAILED / TIMED_OUT / CANCELLED / DELIVERY_UNKNOWN`，清理字段独立记录 `NOT_CREATED / RETAINED / DELETED / CLEANUP_FAILED / CLEANUP_UNKNOWN`。取消及失败可跳过后续生成/发送阶段，统一进入清理；没有创建网页的等待任务可以直接终止。

必须保持以下约束：

- 在一个原子准入操作内检查系统状态、用户未完成任务、执行名额和等待容量，再登记任务。默认第 1 个执行、第 2—6 个等待、第 7 个拒绝。
- 维护 FIFO 等待顺序；拒绝、到期和取消均释放对应用户占位。`queue_capacity=0` 表示不排队，不能直接误用 `asyncio.Queue(maxsize=0)` 的无限队列语义。
- 用户占位键为 `(platform_id, sender_id)`，跨群/私聊共享；直到任务终态及清理结束才释放，避免重入。
- 排队截止时间用单调时钟，从准入开始计算；过期任务应及时移出，即使前一个任务仍在执行。
- **复查建议：** 180 秒只计浏览器提问及生成阶段；消息发送另设 30 秒预算，清理另设 20 秒预算。清理期间仍占执行名额，确保真实标签页操作不超过并发上限。
- 执行超时先取消并等待浏览器任务退出，再做有界清理，最后释放占位；超时后迟到的答案不得继续发送。异常不能永久占用信号量或用户占位。
- 取消在发送开始前生效时，不再提交答案；若平台已接受或发送结果未知，不能承诺撤回。分段发送时停止尚未提交的段，并保留已确认发送的计数。
- 初次准入回复一次任务编号，排队时附当前位置（仅是当时位置，不持续刷屏）；结束时回复答案或失败提示。

### 5.4 清理与恢复

默认不保留时，成功、失败和超时都尝试清理本任务已经创建的会话，随后关闭其标签页。若保留开启，只关闭标签页并保留网页历史。

记录本任务的准确会话标识/URL；删除前核对当前页面归属，严禁靠“侧边栏第一条”猜测，严禁使用“删除所有聊天”。网页删除不代表服务方立即清除全部数据。

**复查建议：** 数据目录保存一个小型 `owned_sessions.json`，只含任务 ID、插件自建会话 ID、创建时间、该任务的保留策略快照和清理状态，无问题或答案正文。先登记归属再进入后续阶段；通过临时文件替换保证原子写入。重启处理仍遵守原任务的保留策略，不能因当前配置改变而删除以前要求保留的会话。页面创建成功但 ID 尚不可读就崩溃时，不能承诺自动定位，应报“清理状态未知”由管理员检查。

清理失败不撤回已经成功的答案，也不把任务重新排队；通知管理员并标记残留。默认暂停新的接单，防止“不保留”模式下继续累积历史；管理员处理后恢复。保留模式不产生删除失败。

登录失效、验证码、限流、浏览器断开或无法识别页面时，停止新准入；等待中的任务终止并给用户一次失败提示，执行任务安全退出。管理员故障通知按同一故障合并。首版不持久化等待队列，重启不重放问题；启动检查自建会话记录并处理已知残留。

**复查建议的管理入口：** `/ask status` 查看就绪/暂停及队列计数；`/ask resume` 在人工处理后重新检查登录、页面和残留，再恢复；`/ask cancel` 取消调用者自己的一个未完成任务。status/resume 只允许宿主超级管理员或本插件配置的管理员，权限独立于使用白名单；不向群内显示账号、会话 URL或其他用户问题。

## 6. 管理员通知与配置

### 6.1 通知语义

通知带任务编号、来源平台/群或私聊标识、错误分类、简短原因、发生时间和合并次数，默认无问题/回答正文。错误文本必须清理 Cookie、Token、完整页面 HTML及敏感路径。

去重键建议 `(platform_id, browser_account_scope, error_category)`；来源信息放正文，避免同一登录失效按每个任务重复私信。首条立即通知；后续 300 秒累计计数，窗口结束补发一次汇总，无新事件则不发。通知失败仅记日志，不能再触发通知递归。

管理员列表保存 QQ ID 字符串，通知通过任务来源 OneBot 实例路由；启动故障没有任务来源时，对已启用的 OneBot 实例各尝试一次。管理员不在好友列表或平台路由失败必须明确记录。空列表允许保存，但面板/日志提示管理员私信未配置，不能宣称“通知成功”。

`Context.send_message()` 的 bool 表示是否找到平台，不是对方已读或最终投递证明；必须同时处理返回 False 和发送异常。`event.send()` 成功返回 None，不能用 `if not await event.send(...)` 判断失败；必须捕获实际 await 的异常。源码未吞掉 OneBot 发送异常，但丢弃了返回数据，页面/客户端最终展示仍需实测。免前缀正则的异常也未必触发宿主默认错误回复，插件必须自己反馈。

### 6.2 配置表

标记“建议”的值是本轮新增默认设计，其他来自已确定需求。

| 字段 | 默认值 | 约束/用途 |
| --- | --- | --- |
| `restrict_groups` / `allowed_groups` | false / [] | 开启且空列表时拒绝所有群；ID 为字符串 |
| `restrict_users` / `allowed_users` | false / [] | 开启且空列表时拒绝所有用户；群聊与群限制取交集 |
| `max_concurrency` | 1 | 正整数；每个名额独立标签页，增加后需验收 |
| `queue_capacity` | 5 | 非负整数；0=不排队 |
| `execution_timeout_seconds` | 180 | 有限正数 |
| `queue_timeout_seconds` | 600 | 有限正数 |
| `max_reply_chars` | 12000 | 至少容纳截断提示 |
| `forward_node_chars` | 1500 | 正整数，不超过总上限；部署时验证平台接受范围 |
| `keep_conversations` | false | 默认删除自建网页会话 |
| `admin_ids` | [] | 按来源平台发送的管理员 QQ 列表 |
| `user_failure_template` | `任务 {task_id} 失败：{reason}。` | 可配置，用户看到的原因经过脱敏 |
| `admin_failure_template` | `[BrowserChat] {category}\n任务：{task_id}\n来源：{source}\n时间：{time}\n原因：{reason}\n次数：{count}` | 可配置，不包含正文 |
| `max_input_chars`（建议） | 12000 | 合并引用与提问后的输入上限，超限拒绝 |
| `browser_channel`（建议） | chrome | 首版验证 Chrome；msedge 需单独验证 |

发送超时 30 秒、清理超时 20 秒、通知窗口 300 秒、完成稳定窗口 2 秒作为有名称的内部常量，首版不为所有内部参数增加面板开关。首版可见浏览器运行，不增加隐藏模式配置。

模板只允许 `{task_id}`、`{reason}`、`{category}`、`{source}`、`{time}`、`{count}` 白名单占位符；禁止表达式求值与属性访问。不合法模板报配置错误，并使用内置安全提示；不把模板错误变成无限通知。

配置在初始化/重载时校验；NaN、无穷大、布尔冒充整数、负值或空白 ID 均不得悄悄通过。任务使用准入时快照；结构性配置变更通过插件重载生效，不能执行中切换 profile 或删除策略。

## 7. 文件与接口划分

只在用户要求开发后创建下列代码文件；本次没有创建它们。

| 文件 | 职责 |
| --- | --- |
| `main.py` | Star 生命周期、过滤器、配置校验、管理命令、任务执行编排 |
| `scheduler.py` | 有界 FIFO、用户占位、消息去重、等待截止时间、取消和停机 |
| `browser_chat.py` | 专用浏览器、登录状态、ChatGPT DOM、生成完成判定、自建会话记录及清理 |
| `messaging.py` | 消息使用权限判定、引用/问题提取、输出截断分段、OneBot 发送、通知模板与合并 |
| `_conf_schema.json` | 配置面板；字段与第 6 节一致 |
| `metadata.yaml` | 名称 `astrbot_plugin_browser_chat`、作者 AceSalm0n、repo URL、初始开发版本、aiocqhttp 平台声明 |
| `requirements.txt` | 经本地兼容性验证的 Playwright 版本范围；不把 AstrBot 整个宿主重复列为插件依赖 |
| `tests/test_scheduler.py`、`tests/test_messaging.py`、`tests/test_browser_chat.py`、`tests/test_plugin.py` | 按行为边界测试 |
| `tests/fixtures/chat.html` | 本地可控页面，模拟逐步生成、错误及完成状态 |

接口草案用于交接，类型均在所属文件定义；实现时若调整，同步调用方和测试，避免多代理各自发明约定：

- `scheduler.py`：`Job` 保存 task_id、去重键、用户键、prompt、来源路由、准入配置快照、时间、阶段/结果/清理状态。`Admission` 保存 accepted、task_id、queue_position、reason。
- `TaskManager(concurrency, capacity, queue_timeout_s, runner)`；`async start() -> None`、`async submit(job: Job) -> Admission`、`async cancel(user_key) -> bool`、`async pause(reason) -> None`、`async resume() -> None`、`snapshot() -> QueueStatus`、`async close() -> None`。runner 为 `async (Job) -> None`，完整覆盖执行、发送和清理；QueueStatus 只含状态、计数及暂停原因，不含其他用户正文。构造不启动任务，initialize 调用 start；start/close 幂等。
- pause 关闭准入、终止等待任务并请求执行任务安全退出；从 runner 内报告故障时不能等待或取消自身而造成死锁，当前 runner 继续走 finally 清理。resume 仅在浏览器检查 READY 且旧任务退出清理后开放准入；close 负责等待退出。管理员命令经 main.py 协调浏览器与调度器，不能只切换一个 ready 标志。
- `browser_chat.py`：`ChatGPTBrowser.start()`、`ask(task_id, prompt, timeout_s, *, keep=False) -> BrowserAnswer`、`cleanup(task_id, keep=None) -> CleanupResult`、`resume(*, account_settings_verified=False) -> BrowserStatus`、`close()` 均为 async。keep 在 ask 前持久化，cleanup 不覆盖原策略；人工账号设置核验才可传 True。`BrowserAnswer` 含 text、conversation_id；异常分类不混入答案文本。失败时归属记录仍可用于 cleanup。BrowserStatus 包含 STARTING、READY、PAUSED、STOPPING，PAUSED 另带 login/challenge/rate_limit/browser/cleanup/config 原因；启动不会自动把未登录状态视为 READY。
- `messaging.py`：`check_access(sender_id, group_id, config) -> bool`、`extract_prompt(event, max_input_chars) -> str`、`split_answer(text, total_limit, node_limit) -> list[str]`；`async send_answer(route, parts, *, timeout_s=30) -> DeliveryResult` 返回 confirmed/partial/unknown/failed、确认段数和 uncertain，confirmed 只表示接口确认接受。SourceRoute.send_handle 使用 OneBotBinding。`async send_failure(route, fault, *, template=None, timeout_s=30)` 为用户提示；`FailureNotifier.notify_failure(platform_id, fault)` 处理管理员合并通知，terminate 调用 close。DeliveryCancelled.result 先记录再传播取消。管理命令另在 main.py 校验管理员身份。
- 模块之间只传必要数据和回调；不为单一站点提前构建多 Provider 注册器、通用任务总线或数据库。来源路由保存 platform_id、会话类型、目标 ID及必要发送句柄，不持久化整个事件对象。

## 8. 当前宿主证据与开发时核查入口

相对路径均以宿主 `AstrBot` 为根，行号仅对应本次快照，改版后重新定位。

| 源码 | 本次能证明的内容 |
| --- | --- |
| `astrbot/__init__.py:3`；`pyproject.toml:7` | 版本 4.28.0；声明 Python >=3.12 |
| `astrbot/core/star/filter/regex.py:9`；`astrbot/core/pipeline/waking_check/stage.py:159`、`:230` | 无前缀仍执行过滤器，匹配后设置 is_wake；不要叠加 command 过滤器 |
| `astrbot/core/platform/sources/aiocqhttp/aiocqhttp_platform_adapter.py:303` | Reply 补全路径通过 get_msg 读取引用 |
| `astrbot/core/message/components.py:584`、`:655`、`:709` | Reply、Node、Nodes 数据结构 |
| `astrbot/core/platform/sources/aiocqhttp/aiocqhttp_message_event.py:127` | 转发经 `send_group_forward_msg` / `send_private_forward_msg`，普通消息走不同路径 |
| 同文件 `:184` | event.send 调用上述发送逻辑；调用无异常不等于终端用户已收到 |
| `astrbot/core/star/context.py:614` | 主动发送路由及 bool 语义；无匹配平台返回 False |
| `astrbot/core/star/base.py:107` | initialize / terminate 生命周期入口 |
| `astrbot/core/star/star_manager.py:2022` | 定义 __del__ 会进入旧分支而跳过 async terminate |
| `astrbot/core/star/star_tools.py:244` | StarTools.get_data_dir 插件持久化目录入口 |

优先复用当前安装源码，不擅自升级整个 AstrBot 或其他插件来满足依赖。全局唤醒前缀和平台启用状态会变化，验收前安全读取相关字段；不要打印完整配置、JWT、Cookie或 QQ 账号清单。

## Global Constraints

- 当前交付只包含文档；功能开发由新对话的明确指令启动。
- 已确定的数值和产品行为以第 3 节为准；新增默认设计标明“复查建议”。
- 仓库保持私有。安装、真实收发、提交、推送和市场发布各按用户实际授权范围执行。
- 所有网络、页面和消息发送均有超时与取消路径；日志无账号凭据及聊天正文。
- 默认 1 执行 + 5 等待；每用户 1 未完成；输入和输出分别受限。
- 实际宿主 venv 为 Python 3.10.6；只声明通过验证的 Python、AstrBot 和平台兼容范围。

## Review Focus

1. 没有生成过答案时，空 DOM 连续稳定不能当成功；在 Task 2 的本地页面测试覆盖。
2. 网页已接受问题但提交确认丢失时不能再次发送；在 Task 2 覆盖不确定提交。
3. QQ 请求超时但可能已经发出时不能自动全文降级；在 Task 3 覆盖不确定投递。
4. 取消/重载与回答完成同时发生时不能发出迟到答案或提前释放并发名额；在 Task 4 覆盖竞争。
5. 配置的保留策略、白名单或模板在执行时改变，不能让旧任务突然改变语义；在 Task 1/4 覆盖配置快照。

## 9. 分阶段实施计划

### Task 0：验证最小浏览器闭环

**文件：** 后续创建 `browser_chat.py` 的启动/关闭部分和 `requirements.txt`；本地实验资料放忽略目录。

**接口：** 产出 `ChatGPTBrowser.start/close` 和可用运行环境记录，供 Task 2 复用。

- [ ] 读取宿主与插件规范、Git 状态、实际解释器和运行循环；确认没有未处理用户修改。
- [x] 只安装与实际 Python 匹配的 Playwright，先验证可见浏览器打开本地页面及关闭。实测 venv 1.58.0 与全局既有 1.60.0，范围限定 `<1.61`，不升级宿主。
- [ ] 获准真实网页联调后，人工登录专用 profile，核验账号设置，验证新建、输入、完成判定、读取和定向删除一个自建测试会话。
- [ ] 若当前网站流程受限或 DOM无法可靠判定，先记录具体阻塞；不开始堆积队列/通知功能掩盖浏览器闭环失败。

**完成条件：** 浏览器启动/退出与取消有证据；真实闭环未获授权或未通过时明确标记未完成，不能据本地页面测试声称已接通 ChatGPT。

### Task 1：准入、引用与输出纯逻辑

**文件：** `scheduler.py`、`messaging.py`、`tests/test_scheduler.py`、`tests/test_messaging.py`。

**接口：** 产出 `Job/Admission/TaskManager`、`check_access`、`extract_prompt`、`split_answer`；TaskManager 的 runner 先用受控协程替身。

- [x] 先写可观察行为测试：两个白名单四种开关组合、私聊规则、引用+问题/仅引用/无文字引用、12,000 输入边界。错误模板按本次范围留 Task 3。
- [x] 调度测试同时提交 7 个不同用户任务，阻塞第一个 runner，断言 `running == 1`、`queued == 5`、第七次 `accepted is False`；同用户跨群再次提交拒绝。
- [x] 输出测试断言 `all(len(p) <= 1500 for p in parts)`、`len(''.join(parts)) <= 12000`，截断提示存在且计入预算；覆盖换行、代码及非 BMP 字符。
- [x] 验证 queue_capacity=0、等待超时、消息重复、取消释放占位、FIFO及配置快照；覆盖 runner 内报告暂停不自锁。测试先因所需行为未实现而失败，再写最小实现并通过。

**验证：** 从插件根目录运行宿主解释器 `-m unittest discover -s tests -p 'test_*.py' -v`，测试不得依赖真实账号或 QQ。

### Task 2：网页生成与清理

**文件：** `browser_chat.py`、`tests/test_browser_chat.py`、`tests/fixtures/chat.html`。

**接口：** 完成第 7 节 `ask/cleanup/resume` 及 BrowserAnswer/CleanupResult/BrowserStatus；输入为 task_id、prompt、timeout，不依赖 AstrBot 事件。

- [x] 本地页面覆盖分批生成、长停顿、始终空白、登录页、限流、错误横幅、未确认提交、选择器失效，先建立失败测试再实现。
- [x] 验证只有本次最终回答能返回；稳定前缀不能误报完成；取消后不再产生可发送结果。
- [x] 验证成功、异常、超时、保留开启、删除失败、会话 ID未知和浏览器关闭后的清理结果；保留设置不改变其他任务。
- [x] 记录所有新会话归属并验证原子写入；重启只检查已知自建记录，遵守记录中的保留策略，不遍历删除账号历史。

**完成条件：** 本地页面全部行为测试通过；真实站点选择器与清理单独验收，不以 fixture 成功替代。

### Task 3：QQ 发送与管理员通知

**文件：** `messaging.py`、`tests/test_messaging.py`。

**接口：** 完成 `send_answer`、DeliveryResult、`notify_failure`；接收来源路由和文本节点，不重新调用模型。

- [x] 模拟群/私聊转发请求，检查路由、机器人署名和节点文本。
- [x] 测试转发明确拒绝时只降级一次；超时则 unknown 且不降级；普通消息发到中途失败时不重发已确认节点。
- [x] 测试多管理员、来源平台错配、返回 False、私信异常、首条立即通知及 300 秒内合并，通知异常不能递归。
- [x] 模板测试覆盖未知占位符、属性访问、换行注入和错误脱敏；用户与管理员文案分开。

**完成条件：** 模拟发送边界通过；真实 QQ 转发和管理员私信由 Task 5 核验。

### Task 4：插件集成与生命周期

**文件：** `main.py`、`metadata.yaml`、`_conf_schema.json`、`tests/test_plugin.py`，更新 `requirements.txt`。

**接口：** main.py 将事件转换为 Job并提交；runner 按 `ask → send_answer → finally cleanup` 编排；initialize/terminate 持有全部资源。

- [x] 模拟与隔离实际过滤链验证精确命令和默认 LLM拦截；权限拒绝不提交浏览器任务，管理权限独立检查。
- [x] 验证生成/发送预算独立、清理期间占槽、取消/卸载/完成竞争及投递结果保留；清理 20 秒截止由已验证 Task 2 模块负责。
- [x] 验证暂停拒绝、排队超时/暂停一次提示、恢复等待旧任务退出和浏览器 READY、旧实例任务取消及新配置快照。
- [x] 开发版元数据/schema、真实 API隔离导入及管理器卸载分支通过；缺依赖保持诊断暂停，不自动安装。
- [ ] 在明确影响范围后核验运行中宿主真实热重载；本轮未执行，不能用隔离生命周期结果替代。

**完成条件：** 模拟生命周期、宿主加载和热重载通过；只改插件文件。

### Task 5：真实联调与交付

**文件：** 更新 `README.md`、必要版本元数据；测试证据中的敏感内容放忽略数据目录。

- [ ] 在获准的测试账号/群里执行第 10 节验收矩阵，记录真实结果；未执行的项目标明原因。
- [ ] 只对本插件运行 Ruff 格式及静态检查、目标 unittest；不在宿主根目录执行全仓格式化。
- [ ] README 写清部署、手动登录、账号设置、全部配置、管理命令、已验证版本及站点变化限制。
- [ ] 更新 AGENTS.md 的阶段状态；由主代理整合测试证据和差异。提交、推送或发布仅在当时授权范围内进行。

建议命令（新增相应代码及工具后才能运行；这些命令本轮未执行）：

```powershell
$pluginPython = 'E:\AstrBotLauncher-0.2.0\AstrBotLauncher-0.2.0\AstrBot\venv\Scripts\python.exe'
& $pluginPython -m unittest discover -s tests -p 'test_*.py' -v
& $pluginPython -m ruff check main.py scheduler.py browser_chat.py messaging.py tests
& $pluginPython -m ruff format --check main.py scheduler.py browser_chat.py messaging.py tests
git diff --check
```

## 10. 验收矩阵与交付标准

| 场景 | 期望结果 | 证据层级 |
| --- | --- | --- |
| 普通 /ask ai | 一次网页提问，一次完整转发回答 | 真实 ChatGPT + QQ |
| 引用+提问、仅引用 | 提交文字符合第 4 节，无图片占位内容 | 模拟 + 真实 QQ |
| 四种白名单组合、私聊 | 精确执行交集和私聊规则 | 单元 + 真实 QQ抽样 |
| 同时 7 人、同人跨群 | 1执行+5等待+1拒绝；同人重复拒绝 | 受控并发；多用户真实抽样 |
| 等待/执行超时 | 失败提示、管理员通知、无迟到回答、名额释放 | 模拟时钟 + 受控页面 |
| 长答案与代码 | 总字符/节点字符均受限，截断标记明确 | 单元 + QQ长内容 |
| 转发拒绝/结果未知 | 前者有限降级，后者不重复发送 | 模拟OneBot异常 |
| 登录失效/验证码/限流 | 暂停接单，清空等待，合并通知，人工处理后恢复 | 受控页面；真实可安全复现部分 |
| 管理员非好友/错平台 | 可诊断失败，无通知循环 | 模拟 + 获准私信 |
| 删除/保留/删除失败 | 只操作自建会话；失败不重发答案 | 本地页面 + 真实自建会话 |
| 插件卸载、热重载、进程崩溃 | 资源释放；无旧问题重放；已知残留可追踪 | 宿主受控测试 |
| 并发配置>1 | 独立标签页、不串答案、清理不误删 | 设置该值时额外真实验收 |

“完成”至少意味着：离线测试与本地页面测试通过、插件可加载、真实 QQ 与 ChatGPT 主流程通过、关键失败路径有证据、README 与配置一致。若真实站点受限，交付应写“代码/模拟已完成，真实接入未通过”，不得标为全部可用。

## 11. 直接复制到新对话的交接提示

```text
请在 E:\AstrBotLauncher-0.2.0\AstrBotLauncher-0.2.0\AstrBot\data\plugins\astrbot_plugin_browser_chat 接手。
先读取本插件 AGENTS.md、适用的上级 AGENTS.md，以及 docs/superpowers/plans/2026-10-15-browser-chat-handoff.md。
这是仓库初始化后的首次开发。仓库保持私有；原有 main 基线为 de5a84a，本地另有 local-init-20261015 备份分支。先查看当前 Git 状态，保留已有文档改动；不要假定没有其他改动。
按交接文档开始开发：先核实实际宿主解释器与浏览器最小闭环，再做权限/队列/输出及插件集成。用户需求与“复查建议”在文档中有明确区分。
保持真实浏览器路线，不调用网页内部接口。首版 ChatGPT、QQ NapCat/OneBot、每请求独立会话；默认1执行+5等待；群/用户白名单；文字引用；合并转发与截断；可配置故障通知；默认删除自建会话。
你是一名 AstrBot 插件开发者，可按任务难度自主选择子代理模型和推理强度，主代理负责最终核验。站点条款限制、登录与页面可用性按文档如实处理，不宣称无限额度或已获网站许可。
需要我手动登录专用浏览器时再提示；真实 QQ 测试先明确本次测试对象。不要自动提交、推送、公开仓库或提交插件市场。完成后区分静态、模拟、宿主加载和真实平台验收结果。
```

本轮仅创建交接文档并更新阅读入口；没有安装依赖、创建功能代码、修改账号设置或发送真实 QQ / ChatGPT 消息。
