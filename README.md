# astrbot_plugin_browser_chat

计划通过专用浏览器操作 ChatGPT 网页，将文字回答返回 QQ 的 AstrBot 插件。后续考虑 Gemini 和 DeepSeek 适配。

**当前状态：已实现 Task 1 纯逻辑及 Task 2 浏览器问答、清理和恢复。本地可控页面与真实 ChatGPT 验收分别记录；真实登录此前受阻，尚无插件入口，暂不可作为完整插件使用，未进行 QQ 测试或市场提交。**

开发前阅读 [AGENTS.md](AGENTS.md) 和 [完整方案与开发交接](docs/superpowers/plans/2026-10-15-browser-chat-handoff.md)。本次仅实施 Task 2，不修改 QQ 发送与调度行为。

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

后续 runner 按 `ask(..., keep=job.config["keep_conversations"]) → send_answer → finally cleanup` 编排；清理前设置 `job.phase = JobPhase.CLEANING`，使用 `CleanupState(result.status.value)` 映射到调度器。恢复准入须先确认浏览器 READY、旧任务退出，再调用调度器 resume。当前没有 main.py 调用方。

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

真实平台仍需完成设置核验及一次自建会话的提问、最终回答读取和定向删除；Task 2 的本地页面测试不替代此项验收。Task 3 的 QQ 发送、Task 4 的宿主集成尚未实施。

## License

AGPL-3.0，见 [LICENSE](LICENSE)。许可证文本沿用现有 `astrbot_plugin_astock_market` 与 `astrbot_plugin_web_check` 的同类约定。
