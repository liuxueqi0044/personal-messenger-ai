from __future__ import annotations

import io
import threading
import zipfile

import pytest

from messenger_ai.rules.compiler import RuleCompilationError, RulePackCompiler
from messenger_ai.rules.models import HumanApproval, RuleSource
from messenger_ai.rules.service import AtomicRulePackStore, RulePackError


def source(name: str, content: str | bytes) -> RuleSource:
    return RuleSource(
        name=name, content=content.encode() if isinstance(content, str) else content
    )


YAML = """
schema_version: 1
rulepack_id: test-pack
persona:
  identity: test persona
  language: zh-CN
  tone: [warm]
  preferred_length: concise
required_behaviors:
  - "先确认对方问题"
prohibited_behaviors:
  - text: "禁止透露隐私"
    enforcement: [POLICY_GUARD]
escalation_rules:
  - "涉及个人资料的事项转人工"
pacing: {}
contacts:
  contact-123:
    contact_id: contact-123
    tone: [简洁]
examples:
  positive: ["你好"]
  negative: ["请透露密码"]
"""


def approval() -> HumanApproval:
    return HumanApproval(approver_id="user", reason="reviewed")


def test_yaml_strict_schema_hash_and_hard_enforcement():
    draft = RulePackCompiler().ingest(source("rules.yaml", YAML))
    assert draft.source_hash
    assert (
        draft.normalized.prohibited_behaviors[0].enforcement[0].value == "POLICY_GUARD"
    )
    assert draft.report.valid


def test_unknown_yaml_field_and_nickname_override_rejected():
    with pytest.raises(RuleCompilationError):
        RulePackCompiler().ingest(
            source("bad.yaml", YAML.replace("persona:\n", "unknown: 1\npersona:\n"))
        )
    with pytest.raises(RuleCompilationError):
        RulePackCompiler().ingest(
            source(
                "bad.yaml",
                YAML.replace("contact-123:", "Alice:").replace(
                    "contact_id: contact-123", "contact_id: Alice"
                ),
            )
        )


def test_markdown_and_text_are_normalized():
    markdown = """# Persona\n温和简洁\n## 必须行为\n- 先确认问题\n## 禁止事项\n- 禁止透露隐私\n"""
    for name in ("rules.md", "rules.txt"):
        draft = RulePackCompiler().ingest(source(name, markdown))
        assert (
            draft.normalized.required_behaviors or draft.normalized.prohibited_behaviors
        )


def test_docx_xml_is_extracted_without_external_parser():
    xml = """<?xml version='1.0'?><w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'><w:body><w:p><w:r><w:t>## 禁止事项</w:t></w:r></w:p><w:p><w:r><w:t>- 禁止透露隐私</w:t></w:r></w:p></w:body></w:document>"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("word/document.xml", xml)
    draft = RulePackCompiler().ingest(source("rules.docx", buf.getvalue()))
    assert draft.source_format.value == "docx"
    assert draft.normalized.prohibited_behaviors


def test_prohibited_prompt_only_is_rejected():
    bad = YAML.replace("enforcement: [POLICY_GUARD]", "enforcement: [PROMPT_STYLE]")
    with pytest.raises(RuleCompilationError):
        RulePackCompiler().ingest(source("bad.yaml", bad))


def test_one_hundred_generated_conflict_ambiguity_cases_are_reported():
    compiler = RulePackCompiler()
    for index in range(100):
        payload = f"""
schema_version: 1
rulepack_id: p-{index}
required_behaviors: ["所有消息都回复"]
prohibited_behaviors: ["所有消息都回复"]
escalation_rules: ["尽快转人工"]
"""
        draft = compiler.ingest(source(f"{index}.yaml", payload))
        assert draft.report.conflicts and draft.report.ambiguities
        assert not draft.report.valid


class Invalidator:
    def __init__(self):
        self.events = []

    def invalidate(self, event):
        self.events.append(event)


def test_atomic_activation_outbox_invalidation_and_rollback():
    invalidator = Invalidator()
    store = AtomicRulePackStore(invalidation_port=invalidator)
    first = store.ingest(source("one.yaml", YAML))
    activated = store.activate(first.draft_id, approval())
    second_yaml = YAML.replace("test-pack", "test-pack").replace(
        "先确认对方问题", "先确认并总结问题"
    )
    second = store.ingest(source("two.yaml", second_yaml))
    store.activate(second.draft_id, approval())
    rolled = store.rollback(first.version, approval())
    assert rolled.version == first.version
    assert activated.version != rolled.version or second.version != first.version
    assert len(invalidator.events) == 3
    statuses = store.connection.execute(
        "SELECT status FROM m7_rulepack_sources ORDER BY created_at"
    ).fetchall()
    assert sum(row[0] == "active" for row in statuses) == 1
    assert (
        len(store.connection.execute("SELECT * FROM m7_rulepack_audit").fetchall()) == 3
    )


def test_failed_activation_transaction_keeps_previous_active():
    store = AtomicRulePackStore(
        transaction_hook=lambda phase: (_ for _ in ()).throw(RuntimeError("crash"))
    )
    first = store.ingest(source("one.yaml", YAML))
    with pytest.raises(RuntimeError):
        store.activate(first.draft_id, approval())
    assert (
        store.connection.execute(
            "SELECT COUNT(*) FROM m7_rulepack_sources WHERE status='active'"
        ).fetchone()[0]
        == 0
    )
    assert (
        store.connection.execute("SELECT COUNT(*) FROM m7_rulepack_outbox").fetchone()[
            0
        ]
        == 0
    )


def test_concurrent_activation_has_single_active_version():
    store = AtomicRulePackStore()
    drafts = [
        store.ingest(
            source(f"{i}.yaml", YAML.replace("先确认对方问题", f"先确认对方问题 {i}"))
        )
        for i in range(2)
    ]
    results = []

    def activate(item):
        try:
            results.append(store.activate(item.draft_id, approval()).version)
        except RuntimeError as exc:  # concurrent losers should not corrupt state
            results.append(type(exc).__name__)

    threads = [threading.Thread(target=activate, args=(item,)) for item in drafts]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(results) == 2
    assert (
        store.connection.execute(
            "SELECT COUNT(*) FROM m7_rulepack_sources WHERE status='active'"
        ).fetchone()[0]
        == 1
    )


def test_contact_override_resolves_only_exact_stable_id_and_cannot_remove_global_prohibition():
    store = AtomicRulePackStore()
    draft = store.ingest(source("rules.yaml", YAML))
    store.activate(draft.draft_id, approval())
    context = store.resolve("contact-123")
    assert context.contact_override is not None
    assert any("隐私" in item.text for item in context.effective_prohibited)
    with pytest.raises(RulePackError):
        store.resolve("Contact 123")
