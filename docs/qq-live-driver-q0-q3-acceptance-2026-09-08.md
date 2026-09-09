# QQ Live Driver Q0–Q3 实现与验收报告

日期：2026-09-08  
范围：V3 前半部分，仅 Q0–Q3 只读链路  
真实发送：0 次

## 结论

Q0–Q3 已完成代码实现、模块级测试和跨模块离线集成，能够把脱敏 QQ UIA 探针报告依次送入：

`Q0 环境哨兵 → Q1 结构映射 → Q3 人工身份绑定 → Q2 只读消息观察`

该链路没有草稿写入、提交或发送接口。真实 QQ 已在最大化后台状态完成三次连续只读复验，Q0 运行环境与 Q1 四个读取角色均得到稳定、唯一结果。当前 exact environment profile 已认证，用户指定的测试会话已形成 pending Q3 application，且 Q2 已完成一次实机只读基线抓取；由于尚缺稳定的第二身份信号，active binding 与自动资格仍为 false，500 条只读夹具验收也尚未完成。因此不能把 Gate A 或这次基线抓取解释为自动聊天或真实发送已经上线。

## 已实现模块

### Q0 环境哨兵

- 严格识别最大化、最小化、普通和未知窗口状态。
- 分开给出观察与提交权限。
- 校验 QQ 版本、可执行文件签名、进程签名、窗口类、DPI、显示器、显示器拓扑、主题、登录状态和模态状态。
- QQ 位于前台时可读但禁止提交；窗口被普通应用遮挡不是自动失败条件。
- 所有未识别状态均失败关闭。

### Q1 结构映射器

- UIA 名称只在本地边界用于语义分类，不进入快照或 selector pack。
- 综合 ControlType、Pattern、祖先关系、受信语义锚点和归一化几何。
- 兼容 QQ 9.9.26 的 Chromium 无障碍树：用左右嵌套窗口和等高重复行识别会话列表、列表项模板与消息区域，不要求标准 `List/ListItem`。
- 几何只能作为次级证据。
- 平分候选、弱候选和缺失候选均拒绝编译。
- selector pack 绑定 QQ 版本、环境指纹、拓扑 digest 和夹具版本。

### Q2 只读消息观察器

- 只接受精确 HWND 范围的只读捕获端口。
- 拒绝全桌面捕获、写操作能力、最小化窗口、陈旧帧和未来帧。
- 会话、方向、时间或观察置信度不足时只产生 pending/quarantine issue。
- 双键去重：`source_evidence_hash + message_watermark`。
- 输出只有只读入站候选，不含发送或回复接口。

### Q3 会话身份绑定器

- 首次绑定、重绑、撤销和回滚都要求显式人工确认。
- 显示昵称和列表位置不参与身份哈希。
- 关键证据包括会话类型、结构路径、参与者签名、可用资料/头像证据、环境指纹和 selector 版本。
- 关键证据冲突会隔离绑定并撤销自动资格。
- 全生命周期保存追加式审计记录。

### Q0–Q3 集成层

- `probe_bridge.py` 严格验证探针的只读和隐私标志。
- `front_half.py` 是无发送面的统一 facade。
- `qq_live_readonly_assess.py` 可运行真实只读评估，但不会自动认证当前环境。
- 候选环境必须人工复核；CLI 永远不会因为一次探针成功就自升为可用能力。

## 自动化验收

| 范围 | 结果 |
|---|---:|
| Q0 | 8 passed |
| Q1 | 8 passed |
| Probe Bridge | 12 passed |
| Q2 + Q3 | 28 passed |
| Q0–Q3 集成 facade/CLI | 9 passed |
| Gate B 精确会话选择编排 | 7 passed |
| Q2 实机抓取端口与 verify-only CLI | 10 passed |
| Q3 稳定资料身份取证 | 15 passed |
| Q3 avatar identity / fallback | 25 passed |
| Live Driver/脚本合计 | 140 collected |
| 全项目 | 409 passed, 1 skipped；2 个已知非失败 warning |
| Ruff | pass |
| Ruff format | pass |
| Python compileall | pass |
| .NET UIA helper build | pass，0 warnings / 0 errors |
| system acceptance | pass |
| B0 实机 UIA avatar discovery | 3 个脱敏 ROI；QQ 保持后台；pending binding 未改 |
| B1 WGC sidecar | managed .NET 10 exact-HWND sidecar（Vortice.Direct3D11 3.8.3）及协议/Python 三阶段/失败关闭测试已接入；实机跨应用 `NO_FRAME`，Q3 仍 pending |

全项目仍有两个非失败 warning：当前执行环境未加载可选 pytest `asyncio_mode` 插件配置，以及 Starlette TestClient 的依赖迁移提醒。

## 当前实机只读结果

运行命令：

```powershell
python scripts/qq_live_readonly_assess.py --max-nodes 5000
```

得到的脱敏结论：

| 项目 | 结果 |
|---|---|
| QQ 版本 | `9.9.26.44343` |
| 窗口状态 | `maximized` |
| 是否前台 | false |
| 登录语义信号 | true（结构化聊天主界面证据） |
| 模态状态 | none |
| UIA 节点 | 桥接后含合成根共 427 个 |
| main window | unique |
| conversation list | unique |
| conversation item | unique（重复行结构模板） |
| message region | unique |
| Q0/Q1 observation ready | true（已绑定认证 profile） |
| candidate runtime suitable | true |
| 连续稳定性 | 3/3 结果一致，拓扑 digest 一致 |
| Gate B 目标状态 | pending application 已保存；active binding=false |
| Q2 当前会话基线 | 6 条可见消息；对方 2、我方 4、unknown 0 |
| Q2 时间状态 | 基线时间未知，全部 pending，不生成自动回复候选 |
| 消息正文输出 | verify-only 输出为 0；正文仅在本地 helper→adapter 管道内存中短暂处理 |
| 窗口状态改变 | 0 |
| 会话选择动作 | 0（目标已在当前右区，`selection_attempted=false`） |
| 真实发送 | 0 |

这证明当前 Windows 权限足以读取 QQ UIA 树；先前问题不是权限未放开，而是 QQ 9.9.26 没有暴露标准 `List/ListItem` 角色。结构模板适配后，Gate A 的“最大化后台只读映射”已经通过。Q2 只读抓取严格限定到 exact HWND，不请求焦点，不改变窗口状态，也不把聊天正文写入 verify-only 输出。

Q3 无导航头像 POC 的实机结果为：`PrintWindow` 返回黑/纯色，随后限定源为 QQ 自身
HWND 的 `GetWindowDC` + `BitBlt` 回退也返回 `EXACT_HWND_CAPTURE_FAILED`。两种调用均未请求
前台、未捕获桌面、未输出或落盘像素。

后续 managed .NET 10 exact-HWND WGC sidecar（Vortice.Direct3D11 `3.8.3`、`UseAppHost=false`、
签名 `dotnet` host）已完成构建并接入 Python `before/capture/after` 三阶段与失败关闭测试。
实机可创建 WGC 帧池/会话，`CaptureService` 能自动启动，`Programmatic` 和 `WithoutBorder` 权限
均为 Allow；但 QQ、Chrome、ChatGPT 均没有交付帧。WgcSharp `1.1.1` 的 `WgcOnly` 原始 COM
探针也独立得到 `NO_FRAME`。因此该阻塞归因于主机/OS WGC 帧交付，不能以权限 Allow、会话创建
或编译成功冒充头像 HMAC；pending binding 与自动资格保持不变。

V4 B0 已完成纯 UIA avatar discovery 和 Python before/discovery/after 三阶段编排。实机在 QQ 保持后台时只读发现 3 个脱敏 normalized ROI；该流程不进入像素采集、资料导航、输入或发送，且 pending binding 未改变。B1 已完成 managed sidecar、协议和编排接入，但主机/OS 无帧交付，不能把 B0/B1 解释为 Q3 完成。候选字段曾因从 `set` 无序读取而错位，现已改为稳定顺序读取；任何字段/证据不一致仍会失败关闭。

## 下一 Gate

1. 当前 QQ 9.9.26 的资料窗、PrintWindow 和 exact-HWND BitBlt 均已判定 `NO-GO`。
2. managed WGC sidecar 已实现；下一步先诊断并修复主机/OS 的跨应用 `NO_FRAME` 帧交付故障。Visual Studio Build Tools `17.14.39` 安装已被 Smart App Control/Config CI 经 AppLocker 事件 `8029` 阻止 UCRT x86 MSI（`1625`→`1603`）；安装器仅留下不完整实例和单独的 MSBuild 可执行文件，v143/MSVC/Windows SDK 不存在，自检仍为 `MISSING_TOOLCHAIN`。不关闭 SAC、不重启、不改用桌面复制 fallback。
3. 第二信号与 pending application 一致后，由人工确认建立 Q3 active binding。
4. 采集 Q2 差量只读夹具并完成 500 条错会话归属为 0 的验收，才解锁 L1 本地草稿。
5. Q0–Q3 完成上述验收前，Q4 草稿写入和任何真实发送继续关闭。

补充：Q1 selector 版本已经与动态消息节点、UIA runtime id 解耦。新消息到达仍会改变完整 `tree_digest`，但不会再无故使 Q3 身份绑定失效；角色结构、Pattern、归一化区域或祖先结构变化仍会触发隔离。

## 文件索引

- `src/messenger_ai/adapters/qq/live_driver/environment.py`
- `src/messenger_ai/adapters/qq/live_driver/topology.py`
- `src/messenger_ai/adapters/qq/live_driver/probe_bridge.py`
- `src/messenger_ai/adapters/qq/live_driver/identity.py`
- `src/messenger_ai/adapters/qq/live_driver/profile_identity.py`
- `src/messenger_ai/adapters/qq/live_driver/observation.py`
- `src/messenger_ai/adapters/qq/live_driver/front_half.py`
- `scripts/qq_live_readonly_assess.py`
- `scripts/qq_live_capture_assess.py`
- `scripts/qq_gate_b_bind_assess.py`
- `scripts/qq_q3_profile_assess.py`
- `scripts/qq_uia_readonly_probe.py`
- `scripts/qq_uia_probe_helper/Program.cs`
