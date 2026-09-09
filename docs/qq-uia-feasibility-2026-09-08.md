# QQ 9.9.26 后台 UIA 可行性判定

日期：2026-09-08  
探针：`scripts/qq_uia_readonly_probe.py` / `qq-uia-readonly-v1`  
结论：`large_architecture_likely`

## 判定范围

本次只在 QQ 已登录且窗口最小化的状态下读取 Windows UI Automation 元数据。探针没有激活窗口、选择会话、读取并输出消息正文、写入文本、调用按钮或发送消息。

## 实测证据

- QQ 版本：`9.9.26.44343`
- UIA 根：`ControlType.Pane / Chrome_WidgetWin_1`
- 可枚举节点：347，完整扫描且未截断
- 文本节点：59；说明只读观察不是完全不可行
- `InvokePattern`：337，但主要位于无稳定标识的 Chromium `Custom` 节点
- `ValuePattern`：3；排除整页 Document 和顶部搜索框后，没有可靠的聊天 Composer 候选
- `SelectionItemPattern`：0
- `TextPattern`：0
- `AutomationId`：0
- 精确发送按钮候选：0

## 为什么不是小工具

项目要求在不占用前台、不使用全局键鼠、不依赖昵称猜测目标的情况下稳定工作。当前版本在后台状态缺少稳定会话选择、聊天输入和发送目标；大量匿名 `Custom + InvokePattern` 节点不能直接作为可靠发送依据。若仅凭树序号或坐标操作，QQ 更新、窗口变化或列表滚动都可能导致错联系人和误发，不符合 M2/M9 的失败关闭约束。

因此，后续需要为 M3 Live Driver 单独设计架构和验收策略，而不是直接把几个 UIA 调用塞进现有 Adapter。

## 可复用部分

这不会推翻现有项目。M0–M13 的 Hub、Reply Planner、Policy、Pacing、一次性授权、WebUI、MCP 和安全治理均可继续复用；需要另做的是 QQ 平台桥接层、稳定身份绑定、后台窗口生命周期和真实发送验证。

## 当前闸门

QQ Adapter 继续保持 `running-unverified / quarantined`，所有真实发送能力为 `unsupported`。本次真实发送数为 0。

