# Personal Messenger AI V5 实施状态

本文把“代码已经写入、离线检查已经通过”和“真实 QQ/Windows 仍待验证”分开记录。离线测试通过不等于已经登录 QQ，也不等于真实发送成功。

| 阶段 | 当前状态 | 说明 |
|---|---|---|
| B0 契约与边界 | 已写入，已做离线检查 | WebUI 只通过 `LiveHubFacade` 调用 runtime；生产模式必须显式注入真实 facade。 |
| B1 虚拟机与 QQ 环境 | 实机待验 | 需要记录 Windows、QQ、UIA、VM 版本和窗口配置。 |
| B2 多联系人观察与绑定 | 实机待验 | 至少绑定 A/B/C；身份证据不充分、绑定过期或有缺口时必须保持暂停。 |
| B3 发送账本与执行器 | 实机待验 | 需要用测试联系人验证 operation_id、提交超时、重复调用和 UNCERTAIN 隔离。 |
| B4 记忆、规则与 DeepSeek | 仅有离线契约检查 | DeepSeek key 由用户在客体的密钥存储或运行环境中配置，不要贴到聊天、源码、截图或普通日志。 |
| B5 编排与恢复 | 仅有离线契约检查 | 需要验证重启恢复、全局/联系人暂停、到期任务恢复和多联系人公平调度。 |
| B6 控制 WebUI | Projection 与 AtomicRuntimeControls 已完成，真实 SQLite/Starlette 离线契约检查通过；真实 driver/API/VM 仍待验 | `RuntimeWebUIProjection` 从 RuntimeState、Hub、M10 和可选规则/driver 投影生成常用页面；生产入口缺真实 facade 时直接报错，不回退 Fake。 |
| B7 端到端交付 | 尚未完成 | 需要真实 A/B/C 消息、至少 5 个配置联系人调度检查，以及验收矩阵记录。 |

## 真实 runtime 与 WebUI 接口

生产代码应构造 `LiveHubFacade(runtime)`。runtime 必须实现：

```python
def webui_page(name: str, entity_id: str | None = None) -> dict[str, Any]: ...
def webui_command(name: str, payload: dict[str, Any]) -> dict[str, Any]: ...
```

常用页面名称为 `inbox`、`conversation`、`reviews`、`contacts`、`contact`、`rules`、`pacing`、`adapters`、`incidents`、`audit`、`settings`。联系人快照应提供 `contact_id`、`display_name`、`binding_status`、`binding_expires_at`、`last_observed_at`、`plan_status`、`pause_status`、`health`、`uncertain_operations` 和递增 `revision`。

暂停命令为 `pause`、`resume`、`pause_contact`、`resume_contact`。不确定发送只能按具体 `operation_id` 和 `expected_revision` 进入“确认已查看，保持不自动重发”的人工处理记录；这个动作不把发送结果改成 VERIFIED，也不批量清除联系人下的其他 operation。

当前已用三个真实 RuntimeState 联系人和 SQLite HubStore 验证联系人列表、单联系人暂停、版本冲突，以及 uncertain operation 的查看记录。该记录只写 WebUI review 表，保留 Hub `send_operations.status=uncertain`，不推进 M10。

离线演示必须显式传入 `FakeHubFacade()`（或 `create_app(..., demo=True)`）。演示联系人会标注 `demo-only`，不能当作真实 QQ 状态。

## 操作注意事项

- DeepSeek API key 只在客体的密钥存储或运行环境中配置，不要把 key 发到聊天，也不要写入源码、fixture、截图或普通日志。
- QQ 绑定必须从当前可见目标会话建立并记录身份依据；昵称单独不足以唯一识别联系人。无法区分时保持该联系人暂停。
- 全局暂停停止所有自动发送；联系人暂停只影响该联系人。绑定过期、健康异常、身份缺口都要在页面可见，并阻止该联系人继续自动发送。
- `UNCERTAIN` 表示无法证明这一次操作是否已经发送。人工“确认已查看”只记录处理，不制造发送成功证据，也不提供自动重发按钮。
- 本轮没有执行真实 QQ 登录、真实 QQ 发送、付费 API 调用或规则激活。
