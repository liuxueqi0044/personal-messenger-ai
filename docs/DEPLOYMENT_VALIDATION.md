# 部署核验记录

## 当前状态（2026-09-10）

更新 wheel 已安装并通过 provider hash match；此前 DeepSeek HTTP 400（JSON Schema `date-time` format 位于 anyOf）已由 DeepSeek-only schema 修复解决。v4 guest 合成 DeepSeek 生成实测通过：`schema_valid=true`、错误为空、usage `895/286/1181`。该结果不等于 runtime WebUI 已启动，也不等于 QQ 真实收发或自动回复完成。身份类型绑定生产接线、guest composer/UIA 路径、多联系人实机验收和群聊范围仍待处理。

下文早期段落是历史快照，保留原证据，不作为当前 guest 安装状态。

已执行 PowerShell parser：全部 deployment 脚本无语法错误；`guest_bootstrap.ps1 -ProjectPath <本项目> -WhatIf` 仅输出计划。FakeSecretStore 密钥配置测试通过。

VirtualBox VM 已完成 Windows 11 家庭版、Guest Additions 和主要来宾控制验收：6GB、4 CPU、80GB 动态 VDI、EFI/TPM 2.0、NAT、clipboard disabled、drag-and-drop disabled。安装使用已校验的简体中文 25H2 x64 v2 ISO，用户已授权 Windows 许可和无产品密钥路径。未登录 QQ、未部署 DeepSeek、未写入真实密钥、未调用 API。VM 当前 `poweroff`，安装 ISO 与辅助 VISO 已移除；VDI 实占 `19354615808` bytes（约 18.03GiB）。

启动时已实测通过 `start_qq_vm.ps1 -Start` 的 13GiB 内存保护；该保护没有关闭用户应用或降低阈值。关机前，Guest Additions 为 `7.1.18 r173720` / RunLevel `3`，`VBoxService` 正在运行，且经 DPAPI 恢复的临时受限密码文件成功用于 GuestControl 非敏感 `whoami`，之后已删除。凭据状态只保留 ACL 为 `<local-user>` 与 `SYSTEM`（不继承）的 `administrator.dpapi`、`pmai_setup.dpapi`；`install\aux` 已删除。来宾为 Windows 11 Home（EditionID `Core`）build `26200`；Disk 0 分区为 EFI 300MiB、MSR 16MiB、Windows 80000MiB、Recovery 1602MiB。AutoAdminLogon、DefaultPassword、DefaultUserName、AutoLogonCount 和 ForceAutoLogon 均已不存在。WinRE 的 `\Recovery\WindowsRE` 路径与 offset `84218478592` 对应分区 4，但未直接验证 Enabled 状态；`C:\Windows\Panther\unattend.xml` 仍存在，非提升删除被拒绝，且未检查敏感字段。
### 只读采集联系人绑定证据

在 VirtualBox 客体内安装依赖并登录 QQ 后运行：

```powershell
python scripts/inspect_qq_guest_bindings.py --selector-pack .\config\qq-selectors.json --output .\binding-candidates.json
```

命令只读取 UIA 树，不选择会话、不写输入框、不发送。输出的
`platform_conversation_id`、`participant_signature` 和树摘要必须由用户人工核对后才能写入配置；
没有稳定 AutomationId 时返回 `binding_not_certifiable`，不会生成健康能力或激活 RulePack。
