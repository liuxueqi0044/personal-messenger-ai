# V5 离线验收记录

本轮离线验收以真实共享 RuntimeApplication、Hub、Memory、M7 规则、M8 planner、M9 授权、M10 pacing 和 QQ VM bridge 运行，不以单独 mock sink 代替。冻结全量结果为 **534 passed，1 warning，23.34s，无 skip**；warning 是 Starlette TestClient/httpx 弃用提示。`git diff --check` 通过。

已验场景：三联系人生成不同 reply 并各自 VERIFIED；五联系人 round-robin 观察；A 模型 Future 悬挂时 B 继续观察/规划；pause/resume 丢弃旧模型结果；三段消息依次 VERIFIED；第一段后人工接管取消余下两段；IGNORE 零发送；机器人 outbound 按方向进入 Memory/Hub。

实现映射：既有 `hub/`、`memory/`、`rules/`、`llm/`、`policy/`、`pacing/` 是业务权威；新增 `runtime/` 负责投影、协调和恢复，`adapters/qq/vm_driver/` 提供固定 UIA 作业与 bridge。V5 是集成改造，不是从零重写。

两个 CLI `--help` 已通过；源码方式需 `PYTHONPATH=src`，客体 `pip -e` 安装后无需该环境变量。`--check`、缺 key/capability/rulepack 拒绝和生命周期均由 `test_vm_cli` 的 9 个案例离线通过。未完成实机验收：Windows 未安装、ISO 未下载、QQ 未登录；当前 QQ/UIA 版本、真实绑定认证、真实 DeepSeek 调用、真实联系人发送均未执行。B7 不完成。

VM 已由 root 核验为关机状态，4 CPU、6GB、80GB 动态盘（实际 2MB）、TPM 2.0；它不代表 Windows 或 QQ 已可运行。
