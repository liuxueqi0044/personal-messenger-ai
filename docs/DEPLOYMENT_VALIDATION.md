# 部署核验记录

已执行 PowerShell parser：全部 deployment 脚本无语法错误；`guest_bootstrap.ps1 -ProjectPath <本项目> -WhatIf` 仅输出计划。FakeSecretStore 密钥配置测试通过。

未执行下载 ISO、创建或启动 VM、安装 Windows/依赖、QQ 登录、真实密钥写入或 API 调用。客体认证和 UIA 兼容性待实机只读诊断。
