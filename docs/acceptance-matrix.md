# M0–M13 验收矩阵

本文保留历史验收矩阵和当时的测试/租约状态；它不是当前 guest 状态快照。当前 Windows、runtime setup、QQ 登录、API 查询和未完成的多联系人/真实收发状态请以 [`../qq-vm/VM-STATUS.md`](../qq-vm/VM-STATUS.md)、[`../qq-vm/install/DEPLOYMENT-HANDOFF.md`](../qq-vm/install/DEPLOYMENT-HANDOFF.md) 和 [`IMPLEMENTATION_STATUS.md`](IMPLEMENTATION_STATUS.md) 为准。历史测试数量、旧 lease 和旧 UIA 状态不应覆盖新的实机证据。

状态含义：`code` 表示实现存在并通过模块级静态/单元检查；`offline` 表示使用 Fake、SQLite in-memory 或合成数据完成契约验收；`live` 只表示真实 Windows/平台环境已通过对应的独立证据，不会由登录状态推断。

| 模块 | code | offline | live | 当前说明 |
|---|---|---|---|---|
| M0 Domain kernel | PASS | PASS | N/A | 领域契约、时钟、状态机和 replay |
| M1 Local Hub | PASS | PASS | PENDING | SQLite Hub/outbox；未接真实发送 |
| M2 Execution Guard | PASS | PASS | PENDING | 守卫和能力探针已有；需现场证据 |
| M3 QQ adapter | PASS | PASS | PARTIAL | WGC 在完整重启后仍跨 QQ/Chrome/ChatGPT `NO_FRAME`，路线正式停止。人工短期会话租约的核心、WebUI、生产组合与 metadata-only UIA helper 已完成；实机 Q0/Q1/header/structure 与 prepare 页面通过，confirm/status 待用户本地提交。permanent Q3 binding、Q4–Q7 与真实发送关闭 |
| M4 WeChat observation | PASS | PASS | PENDING | 观察路径失败关闭 |
| M5 WeChat sending | PASS | PASS | LOCKED | 当前明确 `CAPABILITY_UNSUPPORTED`，真实发送为 0 |
| M6 Memory | PASS | PASS | N/A | 联系人隔离、证据与保留边界 |
| M7 RulePack | PASS | PASS | DRAFT | 当前个人 RulePack 仍为草稿，未激活 |
| M8 Reply Planner | PASS | PASS | PENDING | Fake 评测 200 rule + 100 injection；未配置 live provider |
| M9 Policy/Auth | PASS | PASS | PENDING | eligibility、due revalidation、一次性授权离线通过 |
| M10 Pacing | PASS | PASS | PENDING | 虚拟时钟、恢复、取消、30 秒长文约束离线通过 |
| M11 WebUI | PASS | PASS | PARTIAL | localhost 工作台已接匿名 QQ 会话租约；首屏立即返回，耗时只读检测由用户按钮触发；不接收 QQ 号，no-store/access-log off；实机 confirm/status 待用户提交 |
| M12 MCP Gateway | PASS | PASS | PENDING | 最小工具集、无裸发送/规则激活；本机 MCP live 配置待补 |
| M13 Security/Governance | PASS | PASS | PENDING | 38 个模块测试、DPAPI 实机回读、92 文件安全扫描和离线 SBOM 通过；真实浸泡未完成 |

## 跨模块控制链

离线链路是：M8 `ReplyPlan` → 测试侧显式 projection → M10 计划与 `DueForRevalidation` → M9 重新验证和一次性授权 → Fake execution sink。M8 的严格模型和 M10 的领域模型不是同一 schema，因此测试明确做 projection，而不是修改业务边界掩盖差异。

这条链不导入 QQ/微信 Adapter，也不执行真实发送。M11 页面动作和 M12 工具都只调用 facade/gateway 领域命令。

最终离线回归为 `455 passed, 1 skipped`；Ruff、format、compileall 与 `scripts/system_acceptance.py` 均为 `pass`。当前有 2 个已知非失败 warning：可选 pytest `asyncio_mode` 配置未加载，以及 Starlette TestClient 的依赖迁移提醒。人工 lease 永久保持 `automatic_eligible=false`；实机 prepare 只证明当前会话 metadata 可稳定读取，不构成 permanent Q3 binding 或发送能力。
