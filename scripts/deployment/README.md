# QQ 客体部署

仅从微软官方 [Windows 11 下载页](https://www.microsoft.com/zh-cn/software-download/windows11) 选择 x64 多版本 ISO、语言和 64 位下载。链接有效期为 24 小时；下载后使用 `verify_windows_iso.ps1 -IsoPath <路径> -ExpectedSha256 <微软页面对应语言的SHA256>` 校验。当前官方页面列出的简体中文 x64 SHA-256 为 `7408581E67BC455EBAAFB9230E531ABF45B1C8864A22114A1B03893F897102E4`。

先运行 `prepare_qq_vm.ps1 -IsoPath 'C:\路径\Windows11.iso' -WhatIf` 审核。确认后它创建但不启动 Windows 11 客体：4 vCPU、6144MB（6GB）内存、80 GB 动态盘、EFI/TPM 2.0、NAT、无剪贴板共享/拖拽。系统安装、激活和 QQ 测试账号登录需另行确认。

当前 `outputs/qq-vm/PersonalMessengerQQ` 已创建为关机空 VM，DVD 为 emptydrive；ISO 到位后再挂载，禁止在此阶段启动。

在客体内运行 `guest_bootstrap.ps1 -ProjectPath <项目路径> -WhatIf` 审核，再移除 `-WhatIf` 创建独立 venv 并安装 `[web,llm,qq-vm]`。运行入口待 Sol 的 runtime assembly 绑定；脚本不会声称已启动运行时。

密钥：在客体运行 `python scripts/deployment/configure_deepseek_key.py --vault <DPAPI目录> --alias deepseek.api_key`。脚本以隐藏输入读取密钥，调用既有 Windows DPAPI SecretStore 保存，只显示 alias，不调用 DeepSeek API，也不把密钥放进命令行参数或日志。

运行时配置可从 `runtime.example.json` 复制。必须填入真实的 selector pack、经过人工确认的 QQ binding、与 selector pack 相同的 guest capability fingerprint，以及已经在 `data_dir/rules.sqlite3` 激活的 M7 RulePack。示例中的 `REPLACE_WITH_*` 值会被入口拒绝，入口不会自动激活草稿规则或创建伪造绑定。

启动前只读检查：

```powershell
$env:PYTHONPATH='src'
python scripts/run_vm_runtime.py --config C:\PMAI\runtime.json --check
```

正常启动会从 DPAPI vault 读取 `deepseek.api_key`，并独立持久化 `runtime.authorization.signing` HMAC key；不会把 API key 放入命令行、配置 JSON、worker 环境或日志。WebUI 与 runtime 使用同一进程的真实共享存储，仅绑定到 loopback。

启动前运行 `start_qq_vm.ps1`（仅检查）。它要求可用宿主内存覆盖客体 6GB 后仍保留 6GiB，并额外保留 1GiB 虚拟化余量；不足时只报告阻止原因，绝不关闭应用。只有显式传入 `-Start` 才会以 headless 方式启动，避免抢占宿主桌面。80GB 动态磁盘不是内存。
