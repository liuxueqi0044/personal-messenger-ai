# QQ 客体部署

仅从微软官方 [Windows 11 下载页](https://www.microsoft.com/zh-cn/software-download/windows11) 选择 x64 多版本 ISO、语言和 64 位下载。链接有效期为 24 小时；下载后使用 `verify_windows_iso.ps1 -IsoPath <路径> -ExpectedSha256 <微软页面对应语言的SHA256>` 校验。当前官方页面列出的简体中文 x64 SHA-256 为 `7408581E67BC455EBAAFB9230E531ABF45B1C8864A22114A1B03893F897102E4`。

先运行 `prepare_qq_vm.ps1 -WhatIf` 审核。确认后它创建但不启动 Windows 11 客体：4 vCPU、8 GB 内存、80 GB 动态盘、EFI/TPM 2.0、NAT、无剪贴板共享/拖拽。系统安装、激活和 QQ 测试账号登录需另行确认。

在客体内运行 `guest_bootstrap.ps1 -ProjectPath <项目路径> -WhatIf` 审核，再移除 `-WhatIf` 创建独立 venv 并安装 `[web,llm,qq-vm]`。运行入口待 Sol 的 runtime assembly 绑定；脚本不会声称已启动运行时。

密钥：在客体运行 `python scripts/deployment/configure_deepseek_key.py --vault <DPAPI目录> --alias deepseek.api_key`。脚本以隐藏输入读取密钥，调用既有 Windows DPAPI SecretStore 保存，只显示 alias，不调用 DeepSeek API，也不把密钥放进命令行参数或日志。
