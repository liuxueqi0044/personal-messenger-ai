# 部署核验记录

已执行 PowerShell parser：全部 deployment 脚本无语法错误；`guest_bootstrap.ps1 -ProjectPath <本项目> -WhatIf` 仅输出计划。FakeSecretStore 密钥配置测试通过。

VirtualBox 关机空 VM 已创建并经独立核验：6GB、4 CPU、80GB 动态 VDI（实际占用 2MB）、TPM 2.0。未执行 ISO 下载、VM 启动、Windows 安装、QQ 登录、真实密钥写入或 API 调用；客体认证和 UIA 兼容性待实机只读诊断。

最终独立 VBox 核验仍为 `poweroff`、6144MB、4 CPU、NAT、clipboard disabled、drag-and-drop disabled；未进行运行内存或 guest 启动测试。

静态预算核算：当前宿主可用物理内存约 10GiB；6GB 客体加 6GiB 宿主保留和 1GiB 虚拟化余量需要约 13GiB。因此该配置应被启动前检查阻止。`start_qq_vm.ps1` parser 已核验 0 errors；未进行实际 guest 启动或运行内存测试。
### 只读采集联系人绑定证据

在 VirtualBox 客体内安装依赖并登录 QQ 后运行：

```powershell
python scripts/inspect_qq_guest_bindings.py --selector-pack .\config\qq-selectors.json --output .\binding-candidates.json
```

命令只读取 UIA 树，不选择会话、不写输入框、不发送。输出的
`platform_conversation_id`、`participant_signature` 和树摘要必须由用户人工核对后才能写入配置；
没有稳定 AutomationId 时返回 `binding_not_certifiable`，不会生成健康能力或激活 RulePack。
