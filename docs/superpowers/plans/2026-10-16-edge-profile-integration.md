# 专用 Edge 资料目录接入实施方案

> **For agentic workers:** 使用 superpowers:executing-plans 按步骤实施。用户于 2026-10-16 已授权最小实施、完成后推送一次，并恢复指定范围的真实 QQ 验收。

**Goal:** 正式插件能够使用本次已验证登录成功的专用 Edge 资料目录。

**Architecture:** 保持 Playwright 可见持久化上下文，由插件启动和关闭专用浏览器。复用现有 browser_channel，增加受限的资料目录名称配置；不同资料目录使用独立会话归属记录，兼容原 Chrome 记录位置。

**Tech Stack:** 现有 Python、asyncio、Playwright、AstrBot 配置；无新增依赖。

**Spec:** [需求和接口基线](2026-10-15-browser-chat-handoff.md)，以及本轮用户确认的 Edge 对照结果。

## 已有证据与边界

- 原插件专用 Chrome 多次无法通过 CF；未完成真实提问或会话清理。
- 用户确认：普通启动的独立 Edge 可以登录；随后 Playwright 使用同一专用目录启动 Edge，保留登录且可正常使用。
- 这支持优先接入当前 Edge 环境，尚不能区分原 Chrome 的浏览器差异、资料状态或其他环境因素。
- 成功目录为宿主插件数据目录下的 `edge_manual_diagnostic`，只含本插件诊断资料，不能替换为个人日常浏览器目录。
- 尚未核验账号记忆、引用历史、自定义指令设置；真实提问、回答提取、删除、保留及 QQ 主流程均未通过验收。
- 用户最新指令已恢复指定范围的真实 QQ 验收；先推送本次改动，再进行真实验收。

## 全局约束

- 保留已有 README 修改、Chrome 资料和会话记录，不移动、复制或导出登录资料。
- 不实现 CDP 接管、代理配置、自动浏览器降级或验证码处理。
- 浏览器故障继续暂停；不因 Edge 登录成功跳过人工设置核验或 READY 检查。
- `/ask`、调度、投递、通知、取消和 finally 清理契约保持不变；不修改 scheduler.py、messaging.py。
- 仅按明确授权操作运行中的插件；重载前说明插件任务和浏览器的实际影响，不重启宿主。

## 复核重点

1. 两个进程同时使用同一资料目录：先关闭本轮诊断上下文，再允许正式插件启动；占用时诊断暂停，不强制杀其他浏览器。
2. 切换资料目录后恢复清理：只加载所选目录的归属记录，不能清理旧 Chrome 或其他目录的会话。
3. 非法目录配置：拒绝路径、分隔符、上级跳转、Windows 保留名称及过长名称；拒绝解析到插件数据目录外的链接目标。
4. 旧配置没有新字段：原 Chrome 继续使用原 profile 和 owned_sessions.json。
5. 已登录但未核验账号设置：仍暂停，不能自动确认或提交测试问题。

## Task A：资料目录配置与会话记录隔离

**Files:** browser_chat.py、main.py、_conf_schema.json、tests/test_browser_chat.py、tests/test_plugin.py。

**Interfaces:**

- `ChatGPTBrowser(data_dir: Path, channel="chrome", *, base_url="https://chatgpt.com/", profile_name: str = "")`。
- 新增配置 `browser_profile_name`，默认空字符串，表示按 channel 选择目录。
- 空名称时 Chrome 使用 `browser_profile`，Edge 使用 `browser_profile_msedge`。
- 显式名称仅允许 1—64 位 ASCII 字母、数字、下划线和短横线，另拒绝 Windows 设备名称；构造参数和配置入口均校验。
- 配置只接受目录名称；实际目录固定在 StarTools.get_data_dir 的插件数据根下。解析后的目标必须仍在该根内。
- 原 `browser_profile` 继续使用根下 `owned_sessions.json`；其他名称使用根下 `owned_sessions.<profile_name>.json`。目录选择和记录选择在构造时固定，不在任务执行期间切换。
- ask、cleanup、resume、BrowserAnswer、CleanupResult、BrowserStatus 的接口不变。

- [x] 先补充失败测试：旧配置/默认 Chrome 路径兼容；默认 Edge 路径；显式诊断目录；非法名称及越界目标拒绝。
- [x] 补充两份 profile 归属记录的隔离测试：Edge 不加载或清理旧 Chrome 记录；旧文件原样保留；重建同一 profile 实例读取其原保留策略。
- [x] 补充 main.py 配置传递和快照测试，缺少新字段时兼容原配置。
- [x] 实现上述最小改动；不增加浏览器适配层，不更换启动 API。
- [x] 运行新增用例及现有浏览器归属/恢复、配置和生命周期相关用例，确认失败转通过；执行改动文件 Ruff check/format 检查。

## Task B：本机配置与有限验证

**Files:** 当前安装的插件配置；README.md、AGENTS.md、原交接文档的当前状态记录。测试辅助脚本保留在忽略目录，不作为运行依赖。

- [ ] 关闭本轮诊断脚本拥有的 Playwright Edge，上下文完全退出后再使用其目录；保留 profile。
- [ ] 当前部署设为 `browser_channel="msedge"`、`browser_profile_name="edge_manual_diagnostic"`。发行默认 browser_channel 仍为 chrome，避免无关用户配置迁移；文档给出当前已验证环境的 Edge 配置。
- [ ] 用临时 Edge profile 和本地受控页面验证一次启动、页面操作、取消/关闭及再次启动，不运行整套无关 QQ 测试，不访问真实账号。
- [ ] 同步文档中的配置项、路径、浏览器支持情况和证据边界；标记“Edge 登录与访问已验证，真实问答和 QQ 尚未验收”。
- [ ] 获得恢复真实验收的指令后，再使用实际宿主解释器进行有限登录持久性检查；需要插件重载时先说明影响。若会话失效，只由用户手动登录。
- [ ] 用户实际核验账号设置后，才允许 `/ask resume 已核验`。后续提问、清理及指定 QQ 对象验收仍按原 Task 5 范围和暂停状态执行。

## 交付条件

本方案实施完成仅表示正式插件能选择并启动已验证的专用 Edge 环境，配置/记录隔离和有限本地回归通过；不表示 CF 根因已经确定，也不表示真实 ChatGPT＋QQ 主流程可用。

实现记录列明文件差异、测试结果、运行宿主是否操作和未执行检查，不自动提交或推送。

## 2026-10-16 实施验证记录

新增资料目录测试 5/5、插件集成 24/24 通过；生命周期 9 项中 8 项通过、1 项旧可选 Chrome 用例跳过，包含临时 Edge 本地页面、取消、关闭和同目录新实例重开。共 37 项通过，1 项有意跳过；Ruff check 和差异空白检查通过。未重跑已有未改动的完整 Chrome 页面及 QQ 替身套件。真实问答和 QQ 留待推送后验证。

独立只读复核发现 Windows 大小写别名可能使旧资料对应新记录文件；新增失败回归后改为按 Path 的平台语义判断旧目录，5/5 资料测试通过。无其他确定严重/重要问题。
