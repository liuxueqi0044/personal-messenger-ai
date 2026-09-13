# QQ 群聊范围与代码缺口

本文是当前代码的只读审查记录。产品范围已确定为全部 QQ 好友私聊，明确排除群聊自动回复；本文保留群聊的历史代码缺口，不表示已实现群聊，也不把好友授权扩展为群聊授权。

## 已有可复用基础

- `src/messenger_ai/adapters/qq/live_driver/identity.py` 的 `ConversationType.GROUP`、`IdentityEvidenceSet`、`ConversationBinding` 和 `IdentityBindingRegistry` 已支持把群会话作为一种身份类型，并提供稳定证据、人工确认、版本化绑定、冲突隔离和审计。
- `src/messenger_ai/adapters/qq/vm_driver/worker.py` 已提供串行 `OBSERVE → PREPARE → COMMIT → VERIFY` UIA 证据链；`QQSelectorPack`、VirtualBox guest guard 和 Hub/runtime 的会话游标、去重、quiet-window、pacing、planner、RulePack 服务可复用。
- `src/messenger_ai/policy/models.py` 已定义 `ConversationType.GROUP`，但这只是策略枚举，不代表群聊已获运行资格。

## 当前传播缺口

- `src/messenger_ai/adapters/qq/models.py` 的旧 `QQConversation` 和 `QQIdentityBinding` 没有会话类型、`group_id`、成员摘要或 sender 身份；`QQIdentityBinding.matches()` 只比较 `internal_id` 与 `participant_signature`。因此 live identity 已识别 GROUP，旧 worker binding 尚未传播该类型。
- `QQBubble` 只有会话、消息 key、方向、文本和 UI digest；没有 `sender_id`/sender signature、作者类型、@mention 目标或 bot 自身标记。`src/messenger_ai/domain/models.py::Message`、Hub `messages` 存储和 `runtime/projections.py` 也按单一 `contact_id` 组织。
- `src/messenger_ai/adapters/qq/vm_driver/worker.py` 的循环防护依赖 bubble 方向和快照比较；群聊还需要稳定 bot 作者识别、非 mention 过滤以及 `group_id + platform_message_key` 去重，避免自身消息再次触发规划。
- 会话类型默认值、持久化和调用模型前的 `group`/`unknown` 阻断现已接入；原先“`build_policy_request()` 默认按 direct 处理”的缺口已修复。生产入口源码现在要求认证后的 direct 类型与 friendship verification，并传播到 `RuntimeState.register`；该接线已由离线测试覆盖，但尚未部署新 wheel。群聊仍必须在缺证据时 fail closed。
- `src/messenger_ai/rules/service.py` 的 active RulePack 机制可复用，但现有 `RuleProjection`/contact override 没有群范围、mention 要求、成员触发范围或群内自循环规则。示例或 draft RulePack 不能充当群聊运行规则。
- memory/context 当前按 `contact_id + conversation_id`；若保存成员级上下文，还缺 sender 维度。首阶段应只保留群会话级最小上下文，避免把成员身份当成联系人记忆。

## 分阶段边界与验收

1. **观察阶段**：只建立群 identity/binding、sender/mention 解析、稳定 message key 和自身消息过滤；不规划、不建 draft、不发送。
2. **mention 草稿阶段**：限定一个已有稳定绑定的群；没有明确 @bot 时忽略，生成结果进入既有 policy/review 流程，验证规则版本、sender provenance、重放和自循环防护。
3. **单群受控发送阶段**：复用 guest foreground 的 prepare/commit/verify，限定单段文本；证明目标群未漂移、outbound receipt 唯一、重复/乱序事件不会重复发送，再考虑扩大范围。

最低验收证据：重启后群 identity/participant signature 稳定；sender 或 mention 缺失时阻断；bot 自身消息不再规划；重复事件只产生一个结果；active RulePack、binding revision、capability snapshot 一致；发送 receipt 唯一且目标仍是同一群；日志不写成员原始隐私内容。
