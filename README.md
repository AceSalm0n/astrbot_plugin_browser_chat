# astrbot_plugin_browser_chat

计划通过专用浏览器操作 ChatGPT 网页，将文字回答返回 QQ 的 AstrBot 插件。后续考虑 Gemini 和 DeepSeek 适配。

**当前状态：已实现 Task 1 准入、调度和消息纯逻辑，可独立测试。Task 0 的真实 ChatGPT 登录仍受阻；尚无插件入口，暂不可作为完整插件使用，未进行 QQ 测试或市场提交。**

开发前阅读 [AGENTS.md](AGENTS.md) 和 [完整方案与开发交接](docs/superpowers/plans/2026-10-15-browser-chat-handoff.md)。本次仅实施 Task 1，不包含网页生成、QQ 发送或宿主集成。

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

## Task 0 环境与接口

- 实测：Windows、Python 3.10.6、已安装 Chrome 155.0.8059.79；venv 的 Playwright 1.58.0 和全局解释器已有的 1.60.0 均通过本地生命周期测试，独立测试进程使用 ProactorEventLoop。
- 进程查询看到本轮 `main.py` 的可执行文件为全局 `Python310/python.exe`，父进程为 cmd；没有注入探测其 `sys.prefix` 或导入路径，因此两套独立测试不能替代实际宿主环境验收。没有在宿主内部探测循环，也没有执行插件装载/重载。沙箱内循环创建挂起，沙箱外独立探针正常，暂不引入线程封装。
- `requirements.txt` 范围为 `>=1.58.0,<1.61`，兼容两个实测版本。只向 venv 新增 Playwright 1.58.0 和 pyee 13.0.1，复用已有 greenlet 3.3.0、typing-extensions 4.15.0；全局 1.60.0 未变更，没有升级宿主或其他插件。
- `ChatGPTBrowser(data_dir: Path, channel="chrome")` 提供异步 `start()`、`close()`。后续集成调用方必须传入 `StarTools.get_data_dir("astrbot_plugin_browser_chat")`；profile 为该目录下的 `browser_profile/`，只使用专用资料。
- `start()` 只启动可见浏览器，不访问站点、不代表登录或账号设置通过，状态保持 `PAUSED/config`。Task 2 再实现 READY 检查及 `ask/cleanup/resume`，当前不提供这些方法。
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

两个环境最终均通过 7 项测试。普通测试包含受控替身及真实 Playwright 驱动的延迟连接回归（不打开浏览器、不联网）；显式启用后额外验证真实 Chrome 的本地页面、取消关闭及重新启动。测试使用临时 profile，不读取日常浏览器资料。Ruff 格式/静态检查、compileall 与 `git diff --check` 通过；开发工具 Ruff 仅安装在忽略的 `data/tooling` 下，未列入运行依赖。

## 真实站点结果与下一阶段

本轮专用 Chrome 打开 ChatGPT 后，用户报告无法通过 Cloudflare 验证。已停止联调并关闭自建窗口，保留专用 profile；未完成登录和账号设置核验，未提交问题或创建/删除测试会话，不绕过验证、不调用网页内部接口。

Task 0 仍需在用户能够正常人工登录的专用环境中完成设置核验及一次自建会话的提问、最终回答读取和定向删除。通过后 Task 2 再复用 `start/close` 并实现 `ask/cleanup/resume`、会话归属记录；当前没有提前开发其他模块。

## License

AGPL-3.0，见 [LICENSE](LICENSE)。许可证文本沿用现有 `astrbot_plugin_astock_market` 与 `astrbot_plugin_web_check` 的同类约定。
