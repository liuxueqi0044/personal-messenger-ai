# QQ Q3 人工会话租约设计

日期：2026-09-09  
状态：代码、离线验收与实机 prepare 完成；confirm/status 待用户本地提交  
桌面控制：0  
真实发送：0

## 1. 决策

本机 Windows `10.0.26200` 在完整内核重启、QQ 最大化以及两种独立 WGC 实现下仍不交付 exact-HWND 帧。项目停止继续增加 WGC 变体，也不降级为 Desktop Duplication、整屏截图、前台导航或资料窗自动化。

Q3 改为“一次人工确认、短期有效、严格限定当前会话”的 session lease。它不声称已经建立可跨重启复用的永久联系人绑定，也不自动切换 QQ 会话。

## 2. 用户流程

1. 用户手动打开 QQ、选择目标聊天并保持窗口最大化。
2. 本地工作台首屏立即展示说明，不读取或保存聊天正文，也不要求填写 QQ 号。
3. 用户点击“检测当前 QQ 会话”；后端执行 Q0/Q1、pending application 和当前会话头只读校验。
4. 后端使用 Windows 用户级 DPAPI 保护的 HMAC key，对当前 scope 与随机 nonce 计算匿名 `lease_identity_hmac`。该值只标识本次申请，不是联系人永久身份。
5. 用户明确确认“当前可见会话就是该联系人”。后端再次执行只读校验；前后 PID、HWND、进程启动时间、会话头 digest、selector 与结构证据一致时，创建短期 lease。
6. WebUI 拒绝 query 或 POST body 中遗留的 `qq_number` 字段，避免把无法验证的号码制造成虚假身份保证。

## 3. Lease 范围

Lease 至少绑定：

- `platform=qq`
- 当前 QQ 进程 ID 与进程启动时间
- exact main HWND
- 当前会话头 digest
- selector pack version 与环境指纹
- pending application digest
- 每次申请随机生成、与当前 scope 绑定的 `lease_identity_hmac`
- 人工 confirmation id、确认时间和到期时间

默认有效期不超过 12 小时，并在下列任一条件出现时立即失效：

- QQ 进程退出或 PID/启动时间变化；
- HWND、会话头、selector、环境指纹或关键结构变化；
- 用户切换聊天；
- 出现验证码、账号警告、模态窗口或身份冲突；
- 工作台撤销 lease；
- 到期或系统重启。

## 4. 能力边界

Lease 只允许当前会话的只读观察和后续本地草稿规划验收。它不直接授予：

- 自动发送资格；
- 自动切换联系人；
- 鼠标、键盘、剪贴板或前台控制；
- 读取资料窗、QQ 本地数据库或进程内存；
- 捕获桌面、窗口图像或保存聊天正文。

`automatic_eligible` 在 Q4/Q5 尚未独立验收前继续为 `false`，真实发送维持为 0。

## 5. 接口

### 打开说明页

`GET /qq/session-lease`

- 立即返回说明、流程和安全边界；
- 不执行耗时 QQ 探测；
- 不接收 QQ 号。

### 创建申请

`POST /qq/session-lease/prepare`

- 需要 session CSRF；
- 运行 Q0/Q1、pending application 和当前会话摘要校验；
- 返回脱敏 challenge、有效范围和 expiry；
- 明确拒绝 QQ 号字段。

### 确认并创建

`POST /qq/session-lease/confirm`

- body 中只接收 `challenge_id`、CSRF、一次性 idempotency key 和明确确认布尔值；
- URL/query 和 body 中都拒绝 QQ 号；
- challenge 单次使用，最长 5 分钟；
- 确认前后重新读取 live scope；不一致则失败关闭；
- 成功只返回 lease id、到期时间和匿名 lease HMAC 前缀。

### 撤销

`POST /qq/session-lease/revoke`

- 需要 CSRF 和 lease id；
- 幂等；
- 立即撤销当前 lease 并追加脱敏审计。

### 查询

`GET /qq/session-lease/status`

- 每次查询都会重新读取 Q0/Q1 和当前会话摘要；
- 任一读取错误或 scope 漂移会永久使当前 lease 失败关闭；
- 只返回状态、lease id、到期时间与固定原因码。

## 6. 独立验收

1. 创建流程不要求 QQ 号；query 或 body 中出现遗留 `qq_number` 字段必须拒绝，且不得进入日志、审计和响应。
2. challenge 过期、重复使用、CSRF 错误、缺少显式确认全部失败。
3. 创建前后 PID/HWND/header/structure 任一变化均不创建 lease。
4. QQ 重启、切换聊天、出现模态窗口、环境漂移和 lease 到期都使只读资格失效。
5. 同显示昵称不构成身份；切换会话会使 lease 失效，同一时刻全局最多一个 active lease。
6. WebUI 仅监听 loopback，响应设置 no-store，敏感 POST 不写访问日志。
7. 1000 次并发/重放测试中 challenge 只消费一次。
8. 全量单元测试、Ruff、compileall 与 system acceptance 通过。
9. 实机阶段只验证只读观察；草稿写入和真实发送仍为 0。

## 7. 实现与当前验收

- 核心状态机、生产组合层、仅回环 WebUI 与 QQ 实机会话读取器已经接入。
- QQ helper 新增 `current_session_inspection`：只输出 PID、进程启动时间、exact HWND、前台/最大化布尔值、会话头摘要和稳定头部结构摘要；聊天文字、联系人名称、QQ 号、坐标和像素均不输出。
- 进程/HWND、pending application、环境指纹、selector、会话头和结构摘要会在 prepare、confirm、status 时重验。
- 服务仅保存匿名 `lease_identity_hmac`；WebUI 拒绝 query/body 中的 QQ 号，设置 `no-store`，访问日志关闭。
- 本地说明页已拆分为快速 GET；耗时的 QQ 只读检测只在用户点击后通过 `POST /prepare` 执行，并在线程池中运行，避免阻塞整个 WebUI。
- 当前全量回归为 `455 passed, 1 skipped`，Ruff、format、compileall 与 system acceptance 均通过；真实发送为 0。
- 一次 `WINDOW_STATE_NOT_CERTIFIED` 是瞬时读取；随后窗口边界、`IsZoomed`、DPI、显示器指纹连续通过，证明用户一直使用的最大化布局有效。读取器只对该已知瞬时状态重试一次，不改变窗口。
- 重启后 current Q1 selector 连续三次稳定，但与旧 pending application 的历史 selector 规范化摘要不同。生产启动显式允许只读 rebaseline：旧 application 的环境、header 和 schema 仍须匹配，当前 Q1 仍须唯一通过；旧文件不重写，当前 selector 被固定进短期 lease。
- 生产 `prepare` 已实机成功，本地页面返回 `200`、`no-store` 且确认表单可用；`confirm/status` 等待用户在本地页面勾选当前会话确认。真实发送仍为 0。

## 8. 后续演进

当 QQ 提供可靠官方 API、可在不抢前台的情况下读取稳定 profile id，或 Windows WGC 环境恢复后，可以把短期 lease 升级为可跨会话重验证的 persistent binding。在此之前，不用显示昵称、列表位置或模型猜测替代稳定身份。
