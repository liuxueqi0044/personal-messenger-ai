# QQ Gate B 实机进度

日期：2026-09-08  
范围：精确会话选择、Q3 身份证据申请、Q2 观察前置条件  
真实发送：0 次

## 当前结论

用户已经用一段消息内容明确指定测试会话。最大化状态下的本地 dry-run 在当前右侧聊天区找到唯一一次匹配，因此目标会话已经打开，不需要再次点击；`selection_attempted=false`。

Gate B 人工确认已生成并保存 pending Q3 application，confirmation id 为 `f9b741e0-9af3-4d28-a967-816bb74c7e75`。该申请没有创建联系人或 active binding，`automatic_eligible=false`。强身份评估明确要求稳定的 `profile_id` 或 `avatar_hash` 作为第二信号；当前 header digest 与消息锚点不足以自动认人。

Q2 exact-HWND 只读抓取已在目标会话完成一次基线：6 条可见消息，对方 2 条、我方 4 条、方向 unknown 0。verify-only 结果不包含正文；由于现有可见消息缺少可信时间，全部只作为 pending 基线，不触发回复规划。

当前会话头部也完成了脱敏结构诊断：可见候选的 `automation_id` 均为空，未发现可作为联系人稳定 ID 的 UIA 元数据或独立头像控件。因此不能把昵称控件、几何位置或 header digest 冒充成 `profile_id/avatar_hash`；这属于 QQ 当前无障碍树的能力缺口，不是 Windows 权限不足。

稳定身份取证模块现已完成代码与离线验收：可尝试打开独立资料窗，仅提取明确标签关联的唯一账号，并用 DPAPI 保护密钥计算 HMAC；恢复失败、前台变化或候选不唯一都不会更新申请。实机复验确认当前 QQ 9.9.26 打开资料窗会切到前台，且 `WindowPattern.Close` 未能证明恢复，返回 `RESTORATION_FAILED`。该路线已判定 `NO-GO`，pending application 仍保持原状。

无导航头像 POC 也已完成：UIA 能定位至少两行对方头像，但 QQ 的 GPU/合成渲染让
`PrintWindow` 返回黑/纯色；限定源为 `GetWindowDC(targetHwnd)` 的 BitBlt 回退同样无法取得
可用帧，返回 `EXACT_HWND_CAPTURE_FAILED`。两次探测均未改变前台、未输出图像、未操作
输入框，申请仍为 pending。继续无前台像素取证需要独立的 Windows Graphics Capture 原生
sidecar，而不是扩大桌面截图范围或降低身份门槛。

V4 B0 已补齐纯 UIA avatar discovery 和 Python before/discovery/after 三阶段编排：实机在 QQ 保持后台时只读发现 3 个脱敏 normalized ROI，未读取像素、未导航、未改变 pending binding。该结果只证明安全 ROI 发现可用，不是稳定头像 HMAC、Q3 完成或发送能力。B1 已接入 managed .NET 10 exact-HWND WGC sidecar（Vortice.Direct3D11 `3.8.3`、`UseAppHost=false`、签名 `dotnet` host）及协议/Python 三阶段/失败关闭测试；帧池/会话、CaptureService 和 `Programmatic`/`WithoutBorder` Allow 都可取得，但 QQ、Chrome、ChatGPT 都为 `NO_FRAME`，WgcSharp `1.1.1` `WgcOnly` 原始 COM 探针也相同，故判为主机/OS WGC 帧交付阻塞。

## 本轮完成

- 新增 stdin-only 的精确会话匹配模式，选择短语不进入 argv、JSON 或落盘文件。
- 默认只 dry-run；只有显式授权且列表中恰好一个目标时才允许调用一次 UIA `InvokePattern`。
- 当前右侧区域已经唯一匹配时不会重复选择或 Invoke。
- 不使用鼠标、键盘、剪贴板、窗口抢焦点或前台切换 API。
- 输出仅包含计数与聚合 SHA-256 证据，不输出联系人名称或聊天正文。
- pending application 不创建联系人、不建立 active binding，也不获得自动执行资格。
- 新增 exact environment profile：`profiles/qq/qq-9.9.26-local-certified.json`。
- 修复动态消息导致 selector pack version 漂移的问题。
- 新增 exact-HWND Q2 当前会话抓取端口和 verify-only CLI；不提供任何输入或发送接口。
- 新增 Q3 稳定资料身份取证：低熵 QQ 号只生成 HMAC，不输出或保存原值。
- 新增 B0 UIA avatar discovery-only：输出 3 个脱敏 normalized ROI 和结构摘要；Python 三阶段编排在 before/after 一致时仍只保留 pending 申请。
- 新增 managed B1 exact-HWND WGC sidecar，使用 Vortice.Direct3D11 `3.8.3`；0 warning / 0 error，固定协议、Python 三阶段编排和失败关闭测试均已接入。修复 Python 从 `set` 无序读取 candidate 字段导致的错位，改为稳定顺序读取。
- Build Tools `17.14.39` 官方包下载/签名校验成功，但 Smart App Control/Config CI 经 AppLocker MSI and Script 事件 `8029` 阻止 Microsoft Universal CRT Redistributable x86 MSI（`1625`，外层 `1603`）；未关闭 SAC、未重启。安装器留下不完整实例和单独的 MSBuild 可执行文件，但 v143/MSVC/Windows SDK 不存在，项目自检仍为 `MISSING_TOOLCHAIN`。

## 失败关闭条件

- QQ 非唯一、最小化、非最大化或位于前台。
- 未登录、存在登录/验证码/更新/账号风险模态。
- UIA 树被截断或重复会话行结构不可识别。
- 指定短语在全局或当前右区不是唯一一次命中。
- 列表候选为零个或多个。
- 隐私标志、证据 digest 或 dry-run/authorized 证据不一致。

## 验收状态

| 项目 | 结果 |
|---|---:|
| Gate B Python 编排测试 | 7 passed |
| Q2 抓取端口与 verify-only CLI | 10 passed |
| Q3 profile identity 新增测试 | 15 passed |
| Q3 avatar identity / fallback | 25 passed |
| QQ Live Driver/脚本 | 140 collected |
| 全项目 | 409 passed, 1 skipped；2 个已知非失败 warning |
| Ruff / format / compileall | pass |
| .NET helper build | pass，0 warnings / 0 errors |
| system acceptance | pass |
| B0 实机只读 discovery | 3 个脱敏 ROI；QQ 保持后台；pending binding 未改 |
| B1 managed WGC sidecar | 实现/协议/编排完成；帧池/会话可创建但跨 QQ/Chrome/ChatGPT `NO_FRAME`，Q3 pending |
| 系统离线验收 | pass |
| 会话选择动作 | 0（目标已打开） |
| 输入动作 | 0 |
| 真实发送 | 0 |

## 下一动作

1. 保持资料窗、PrintWindow、exact-HWND BitBlt 三条已失败路线为 `NO-GO`。
2. 先在主机/OS 层诊断跨应用 WGC `NO_FRAME`；不关闭 Smart App Control、不重启、不使用 WgcSharp desktop-copy fallback 或其他桌面采集替代方案。
3. 稳定证据一致后仍要求人工确认，才创建 active binding；任何冲突继续隔离。
4. 只观察确认后的新消息差量，完成 500 条错会话归属为 0 的验收后，才考虑解锁 L1 本地草稿。
