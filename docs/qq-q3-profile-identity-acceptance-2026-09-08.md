# QQ Q3 稳定身份取证模块验收

日期：2026-09-08  
范围：当前会话资料取证、低熵身份保护、pending application 更新  
输入或发送：0 次

## 结论

Q3 稳定身份取证模块的代码与离线验收已经完成。模块会先复验认证 Q0/Q1 环境，再通过当前唯一会话标题的 UIA `InvokePattern` 在后台临时打开资料窗；仅当资料窗是同一 QQ 进程新建、能够用 `WindowPattern.Close` 安全关闭，并且存在唯一、明确标注的 QQ 号/账号值时，才生成稳定身份摘要。

QQ 号属于低熵标识，模块禁止输出或保存原值，也不接受普通 SHA-256。身份值使用 Windows 用户级 DPAPI 保护的本机密钥计算 HMAC-SHA256；密钥只通过 stdin 传给 helper，不进入 argv、日志或 JSON。

实机复验经历了两级失败关闭。首次运行时 QQ 已最小化，Q0 在导航前停止；重新最大化后，Q0/Q1 恢复 ready，资料标题调用确实打开了 QQ 资料窗，但 QQ 同时切到了前台，而且 `WindowPattern.Close` 未能在恢复验证窗口内证明资料窗已关闭，返回 `RESTORATION_FAILED`。随后的只读 Q0 检查仍看到 `modal_present` 且 QQ 位于前台。

因此，当前 QQ 9.9.26 的“通过资料窗取稳定 ID”路线判定为 `NO-GO`：它会争夺前台且无法由无键鼠路径可靠恢复。模块没有输出身份 HMAC、没有更新 pending application，也没有创建绑定或发送消息。用户需要手动关闭资料窗；后续改走不导航的 exact-HWND 头像哈希取证 POC。

## Avatar fallback 契约

头像路线已经完成 Python 消费层和离线契约验收。helper 可以报告 `PrintWindow`、
`WindowsGraphicsCapture` 或 `ExactHwndBitBlt`，但三者必须同时满足 exact HWND、QQ 进程与窗口状态匹配、窗口最大化、前后台状态均未改变、无桌面捕获、无图像字节/聊天文字输出、无输入/剪贴板/发送访问，并且至少两行头像产生同一个 64 位 keyed pixel fingerprint。`ExactHwndBitBlt` 的源必须是 `GetWindowDC(targetHwnd)`，禁止 `GetDC(0)` 或任何桌面 DC。头像 HMAC 只进入脱敏 `IdentityEvidenceSet.avatar_hash`，仍然不会创建联系人或 active binding。

本次实机验证记录：QQ `9.9.26` 的 `PrintWindow` 返回
`PRINTWINDOW_BLACK_OR_UNIFORM`。加入 `GetWindowDC(targetHwnd)` + `BitBlt` 的 exact-HWND
回退后，实机仍返回 `EXACT_HWND_CAPTURE_FAILED`。两种传统 HWND/GDI 采集方式在该版本上
均判定为 `NO-GO`。这不是权限不足，也不代表 Q0–Q2 或整个项目失败；它只说明当前小工具
无法从新版 QQ 的 GPU/合成表面取得头像像素。pending application 全程保持原样。

真正的 `WindowsGraphicsCapture` 需要 C#/WinRT 投影、D3D 帧读取和独立资源生命周期。
当前机器的 .NET helper 工具链不能安全直接引用 WinMD；最小官方依赖试验已回退，基线工程
仍为 0 warning / 0 error。若继续，应把 WGC 做成隔离的原生 sidecar 子项目并单独验收，
不能在现有 helper 中手写高风险 COM/D3D 或降级为桌面截图。

V4 B0 已完成纯 UIA avatar discovery 与 Python `before/discovery/after` 三阶段编排：QQ 保持后台时
实机只读发现 3 个脱敏 normalized ROI，pending binding 未改。该只读发现不提供头像 HMAC，不能
解除本报告所述 Q3 身份阻塞。B1 仅有 native sidecar build skeleton；本机缺 VS2022 v143、MSBuild、
MSVC x64 和 Windows SDK 10.0.26100，原生 WGC core 阻塞且未实现。

## 模块契约

- 精确唯一 QQ HWND、已登录、最大化、非前台、无模态窗口。
- live environment fingerprint 与 selector pack 必须和 pending application 一致。
- 当前 header digest 必须与用户已确认的目标会话一致。
- 资料入口最多调用一次，不使用鼠标、键盘、剪贴板或置前 API。
- 只接受明确标签内联数值，或同父同行 label/value 数值；去重后必须唯一。
- 资料窗必须恢复，且前台窗口不得变化；否则不输出 HMAC、不更新申请。
- 成功后也只把申请推进到 `pending_human_binding_confirmation`；不创建联系人、active binding 或自动资格。

## 验收结果

| 项目 | 结果 |
|---|---:|
| 新增 Q3 profile identity 测试 | 15 passed |
| Q3 avatar identity / fallback 契约测试 | 25 passed |
| QQ Live Driver/脚本测试合计 | 140 collected |
| 全项目 | 409 passed, 1 skipped；2 个已知非失败 warning |
| Ruff / format / compileall | pass |
| .NET helper build | pass，0 warnings / 0 errors |
| system acceptance | pass |
| B0 UIA discovery | 3 个脱敏 ROI；QQ 保持后台；pending binding 未改 |
| B1 WGC sidecar | build skeleton only；原生 core 阻塞 |
| 实机前置条件 | 重新最大化后 Q0/Q1 ready |
| 实机资料导航 | 已执行；`RESTORATION_FAILED` |
| 前台争用 | 发生，当前路线 `NO-GO` |
| pending application | 未改变，automatic_eligible=false |
| 实机 PrintWindow 头像 | `PRINTWINDOW_BLACK_OR_UNIFORM`，NO-GO |
| 实机 exact-HWND BitBlt | `EXACT_HWND_CAPTURE_FAILED`，NO-GO |
| Windows Graphics Capture | 需独立原生 sidecar，尚未实机验收 |
| 输入或发送 | 0 次 |

## 主要文件

- `src/messenger_ai/adapters/qq/live_driver/profile_identity.py`
- `scripts/qq_q3_profile_assess.py`
- `src/messenger_ai/adapters/qq/live_driver/avatar_identity.py`
- `scripts/qq_q3_avatar_assess.py`
- `scripts/qq_uia_probe_helper/Program.cs`
- `tests/adapters/qq/live_driver/test_profile_identity.py`
- `tests/scripts/test_qq_q3_profile_assess.py`

## 下一步

1. 不再重试资料窗、PrintWindow 或 exact-HWND BitBlt；三者在当前 QQ 版本均为 `NO-GO`。
2. 若继续无前台头像取证，先建立隔离的原生 Windows Graphics Capture sidecar 模块。
3. sidecar 必须通过 exact-HWND、零桌面捕获、零像素落盘/输出、零前台变化和资源泄漏验收。
4. 在稳定身份信号实机通过前继续保持人工 pending，不降低身份标准。
