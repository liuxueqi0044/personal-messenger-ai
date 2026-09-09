# Personal Messenger AI

这是一个本地、失败关闭的个人消息辅助系统原型。M0–M13 已有代码或离线验收产物，但这不等于 QQ/微信真实自动发送已经解锁。

最新实施方案见 [`../personal-messenger-ai-architecture-v4.md`](../personal-messenger-ai-architecture-v4.md)。V4 保留现有 M0–M13 和 QQ Q0–Q7 边界。跨应用 Windows Graphics Capture 在完整重启后仍不交付帧，已正式停止；Q3 改用人工确认的短期会话租约。QQ 由用户手动最大化并选择聊天，程序不置前、不切换会话、不模拟键鼠。

## 快速开始

需要 Python 3.12。项目默认只允许本机回环地址，不开放局域网端口。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[test,web]"
python -m pytest -q
```

启动本地工作台：

```powershell
python scripts/run_local_webui.py
# 浏览器打开 http://127.0.0.1:8765/inbox
```

启用 QQ 人工会话租约（仍不会发送消息）：

```powershell
python scripts/run_local_webui.py --enable-qq-session-lease
# 用户手动选中目标聊天并最大化 QQ，然后打开：
# http://127.0.0.1:8765/qq/session-lease
```

不需要填写对方 QQ 号。程序使用 Windows 用户级 DPAPI 保护的密钥，为当前窗口、当前聊天证据和随机 nonce 生成匿名短期租约标识；它不是联系人永久身份，也不能跨会话复用。租约最长 12 小时，QQ 重启、会话切换、窗口/环境/selector 漂移、读取错误或人工撤销都会使其失效；`automatic_eligible` 始终为 `false`。

Docker 不是必需依赖，也不能控制 Windows 上的 QQ 或微信客户端。QQ 已登录只代表客户端登录，不代表后台观察、发送或验证能力已经认证。

## 当前边界

- 工作台和 MCP 只调用 Hub/facade 领域命令，不提供裸 `send_message` 或规则激活工具。
- M8 模型只输出结构化 `ReplyPlan`，M9 负责确定性资格与一次性授权，M10 只产生到期复核事件。
- 忽略计划不会创建发送计划；长回复（至少 60 个中文字符）首发至少等待 30 秒，最多 3 个语义分段。
- 不使用 QQ/微信私有协议、Hook、注入、内存读取、全局输入或验证码绕过。
- 当前 RulePack 仍是 `DRAFT`；真实 QQ L2/L3 后台发送尚未解锁。
- 跨模块和系统验收均停在 Fake/in-memory execution sink，绝不真实发送。

详细分层状态见 [`docs/acceptance-matrix.md`](docs/acceptance-matrix.md)。

## 离线验收

```powershell
python scripts/system_acceptance.py
python scripts/run_llm_evals.py --json
python scripts/policy_matrix.py
python scripts/pacing_audit.py
python scripts/run_mcp_gateway.py
python scripts/qq_live_readonly_assess.py --max-nodes 5000
```

完整静态检查：

```powershell
ruff check src tests scripts
ruff format --check src tests scripts
python -m compileall -q src scripts
```

## 模块状态

- M0–M7：领域内核、Hub、后台守卫、QQ/微信适配边界、联系人记忆和 RulePack。
- M8：严格结构化 Reply Planner；200 条规则样例和 100 条提示注入样例为合成契约评测。
- M9：确定性策略、到期重验证和 HMAC 一次性授权；64 路并发只允许一个消费者成功。
- M10：SQLite 可恢复节奏调度；只产生复核事件，不签授权、不发送。
- M11：仅回环地址的本地工作台；当前用 FakeHub 完成页面和安全验收。
- M12：最小权限 MCP Gateway；无裸发送、无规则激活、无 Adapter 依赖。
- M13：DPAPI 密钥、日志脱敏、证据 TTL、指标白名单、版本降级、事故隔离、恢复保护和 SBOM。
- M3/Q0–Q3：只读环境哨兵、脱敏 UIA 拓扑、消息观察、人工会话租约和统一只读 facade 已完成代码/离线验收。租约 helper 只输出当前 PID/HWND/启动时间与会话头、稳定结构摘要；没有聊天文字、像素、写入或发送接口。

当前个人规则包位于 `rulepacks/personal-default-v1.yaml`，状态仍是 `DRAFT`，不能按已激活规则处理。

## 当前机器能力

| 能力 | 当前状态 |
|---|---|
| QQ `9.9.26.44343` | WGC 经完整内核重启和独立 WgcSharp 探针仍为 `NO_FRAME`，路线已停止。人工会话租约核心、WebUI、生产组合和 metadata-only UIA helper 已完成；最大化状态与目标 header 已实机通过，生产 prepare 页面返回 200。confirm/status 等待用户在本地表单确认；Q3 permanent binding 仍 pending，发送关闭 |
| 微信视觉观察 | 未取得新的真实窗口证据，保持失败关闭 |
| 微信后台发送 | `CAPABILITY_UNSUPPORTED`；真实发送 0 次 |

QQ 真实 L2/L3 仍需 Q4 后台草稿硬闸门、500 条只读夹具、专用测试账号 100 次发送零安全错误，以及 7 天浸泡；微信 L3 需要 14 天浸泡。不得使用主账号完成发送 POC。

最新只读 UIA 判定见 `docs/qq-uia-feasibility-2026-09-08.md`：最小化状态可读取部分文本结构，但缺少稳定会话选择、聊天输入和发送目标，因此完整后台驱动不是一个小补丁，需要按 V3 单独验收。窗口最大化能减少布局漂移，但不能替代稳定身份、后台写入和发送后验证。

Q0–Q3 的原始实现与验收结果见 [`docs/qq-live-driver-q0-q3-acceptance-2026-09-08.md`](docs/qq-live-driver-q0-q3-acceptance-2026-09-08.md)，人工租约见 [`docs/qq-q3-manual-session-lease-design-2026-09-09.md`](docs/qq-q3-manual-session-lease-design-2026-09-09.md)。当前全量回归为 `455 passed, 1 skipped`，有 2 个已知非失败 warning；Ruff、format、compileall 和系统验收通过，真实发送仍为 0。

主机构建依赖未就绪：官方 Visual Studio Build Tools 2022 `17.14.39` 安装包下载与签名校验均成功，
但安装以 `1603` 结束。AppLocker MSI and Script 日志事件 `8029` 表明 Smart App Control/Config CI
阻止了 Microsoft Universal CRT Redistributable x86 MSI（内部返回 `1625`）。SAC 未关闭；完整内核重启
后 v143/MSVC/Windows SDK 仍未安装，WGC 也仍不交付帧。系统不会使用 WgcSharp 的桌面复制 fallback，
也不会输出或落盘像素、改变 pending permanent binding 或发送消息。

## 包入口

- `messenger_ai.domain`、`hub`、`execution_guard`
- `messenger_ai.adapters.qq`、`adapters.wechat.observation`、`adapters.wechat.sending`
- `messenger_ai.memory`、`rules`、`llm`、`policy`、`pacing`
- `messenger_ai.webui`、`mcp_gateway`、`observability`

治理材料位于 `docs/`，其中包含威胁模型、隐私模型、兼容矩阵、事故手册、M13 验收报告和离线 SBOM。当前项目是代码/离线验收完成的失败关闭原型，不是已经解锁的无人值守聊天产品。
