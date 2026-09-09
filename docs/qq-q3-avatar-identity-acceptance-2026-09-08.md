# QQ Q3 无导航头像身份 POC 验收

日期：2026-09-08  
目标版本：QQ `9.9.26.44343`  
真实发送：0 次

## 结论

POC 已完成代码、离线测试和两种 GDI exact-HWND 采集方式的实机验证，但在当前 QQ 版本上
不能取得可用头像像素。后续 managed .NET 10 exact-HWND WGC sidecar（Vortice.Direct3D11
`3.8.3`）已构建并接入协议/Python 三阶段/失败关闭测试，却在 QQ、Chrome、ChatGPT 上都收到
`NO_FRAME`；WgcSharp `1.1.1` 的 `WgcOnly` 原始 COM 探针独立复现。因此当前判定为主机/OS
WGC 帧交付 `NO_FRAME`，Q3 继续 pending，而非 QQ 单版本、权限或编排成功即可解除的限制。
失败范围仅限头像身份第二信号；Q0/Q1 环境与结构映射、Q2 后台只读消息观察仍然有效。

实机顺序：

1. UIA 在当前聊天区定位到至少两行对方头像候选。
2. `PrintWindow(targetHwnd)` 返回黑/纯色帧，状态 `PRINTWINDOW_BLACK_OR_UNIFORM`。
3. 增加 `GetWindowDC(targetHwnd)` + `BitBlt` 回退，不使用桌面 DC；再次实机运行返回
   `EXACT_HWND_CAPTURE_FAILED`。
4. 两次运行均未请求或改变前台、未导航、未触碰输入框、未输出/保存图片、未发送消息。
5. pending application 保持 `pending_human_binding`，`binding_created=false`，
   `automatic_eligible=false`，没有身份 evidence/hash。

## 已实现的安全边界

- 唯一、最大化、后台、已登录、无模态的 QQ HWND。
- 当前 header digest 必须和 pending application 完全一致。
- UIA 节点最多 5,000，头像原始候选最多 256，超过即失败关闭。
- 三帧稳定性；至少两条不同消息行必须得到同一 HMAC。
- HMAC 密钥由用户级 DPAPI 保护，仅经 stdin 进入 helper。
- 图像只在 helper 内存中存在，逐帧清零；stdout 不含像素、头像 HMAC、昵称或聊天正文。
- 禁止 `GetDC(0)`、桌面 BitBlt、鼠标、键盘、剪贴板、前台请求与发送控件访问。

## 验收结果

| 项目 | 结果 |
|---|---:|
| Q3 avatar identity / fallback 定向测试 | 25 passed |
| 全项目 | 409 passed, 1 skipped；2 个已知非失败 warning |
| Ruff / format / compileall | pass |
| system acceptance | pass |
| .NET helper build | pass，0 warnings / 0 errors |
| B0 UIA discovery | 3 个脱敏 ROI；QQ 保持后台；pending binding 未改 |
| B1 WGC sidecar | managed .NET 10 exact-HWND sidecar（Vortice.Direct3D11 3.8.3）已接入；0 warning / 0 error；跨应用 `NO_FRAME` |
| 实机 PrintWindow | `NO-GO`，黑/纯色 |
| 实机 exact-HWND BitBlt | `NO-GO`，无可用帧 |
| 前台变化 | 0 |
| 输入或发送 | 0 |

## 后续架构决定

不再为传统 HWND/GDI 捕获叠加补丁，也不允许退化成桌面截图或 WgcSharp desktop-copy fallback。
已接入的 managed sidecar 通过 exact HWND 创建 WGC 帧池/会话，仅返回 ROI 的 HMAC 与结构摘要；
原始 frame、ROI 图像和聊天正文均不离开进程内存、不写临时文件。实机虽可启动 CaptureService，
且 `Programmatic`/`WithoutBorder` 权限均为 Allow，却无帧交付；须先解决主机/OS 故障并通过
三帧稳定、进程/句柄绑定、无前台变化、500 次无泄漏验收，才可接回 Q3。

## V4 B0/B1 状态

B0 已完成：UIA avatar discovery-only 与 Python `before/discovery/after` 三阶段编排在 QQ 保持
后台的实机只读场景发现 3 个脱敏 normalized ROI。它不采集像素、不产生头像 HMAC、不导航、
不修改 pending binding；因此 Q3 仍未完成。

B1 已完成 managed .NET 10 sidecar、固定协议与 Python 三阶段编排，`UseAppHost=false`，由签名
`dotnet` host 运行。它不包含 Desktop Duplication 或宽范围截图接口。实机 `NO_FRAME` 使头像
HMAC 仍不可用。Build Tools `17.14.39` 官方安装包下载/签名校验成功，但 Smart App Control/
Config CI 通过 AppLocker MSI and Script 事件 `8029` 阻止 Microsoft Universal CRT Redistributable
x86 MSI（`1625`，外层 `1603`）；未关闭 SAC、未重启。安装器留下不完整实例和单独的 MSBuild
可执行文件，但 v143/MSVC/Windows SDK 不存在，项目自检仍为 `MISSING_TOOLCHAIN`。
