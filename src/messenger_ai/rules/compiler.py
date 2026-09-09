"""M7 compiler: strict normalization, enforcement, precedence, reports."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any, ClassVar

from pydantic import ValidationError

from .ingest import extract, markdown_to_mapping
from .models import (
    AmbiguityReport,
    ConflictReport,
    ExecutionMode,
    NormalizedRuleSource,
    Rule,
    RuleKind,
    RulePackDraft,
    RulePackReport,
    RulePriority,
    RuleSource,
    RuleTestCase,
    SourceFormat,
)


class RuleCompilationError(ValueError):
    def __init__(self, message: str, report: RulePackReport | None = None) -> None:
        super().__init__(message)
        self.report = report


_HARD_PROHIBITION = re.compile(
    r"(?:禁止|不得|不允许|不要|不能|严禁|转账|透露隐私|泄露密码|答应见面|promise|transfer money|reveal privacy)",
    re.IGNORECASE,
)
_AMBIGUOUS = re.compile(
    r"(?:尽快|尽量|适当|有时|可能|看情况|敏感(?:话题|事项)?|soon|usually|appropriate)",
    re.IGNORECASE,
)


class RulePackCompiler:
    compiler_version = "m7-1"

    def ingest(self, source: RuleSource) -> RulePackDraft:
        source_hash = hashlib.sha256(source.content).hexdigest()
        source_format, raw = extract(source)
        if source_format in {
            SourceFormat.MARKDOWN,
            SourceFormat.TEXT,
            SourceFormat.DOCX,
        }:
            raw = markdown_to_mapping(str(raw))
        try:
            normalized = self._normalize(raw, source_name=source.name)
        except (ValidationError, ValueError, TypeError) as exc:
            report = RulePackReport(valid=False, errors=(str(exc),))
            raise RuleCompilationError(
                "rule source failed strict schema validation", report
            ) from exc
        report = self._report(normalized)
        if report.errors:
            raise RuleCompilationError("rule source failed compilation", report)
        version = f"v{source_hash[:12]}"
        return RulePackDraft(
            rulepack_id=normalized.rulepack_id,
            version=version,
            source_name=source.name,
            source_format=source_format,
            source_hash=source_hash,
            normalized=normalized,
            report=report,
            compiler_version=self.compiler_version,
        )

    def _normalize(self, raw: object, *, source_name: str) -> NormalizedRuleSource:
        if not isinstance(raw, Mapping):
            raise TypeError("normalized source must be a mapping")
        allowed = {
            "schema_version",
            "rulepack_id",
            "persona",
            "required_behaviors",
            "prohibited_behaviors",
            "escalation_rules",
            "pacing",
            "contacts",
            "examples",
        }
        unknown = set(raw) - allowed
        if unknown:
            raise ValueError(f"unknown top-level fields: {sorted(unknown)}")
        data = dict(raw)
        data["required_behaviors"] = self._rules(
            data.get("required_behaviors", []),
            RuleKind.REQUIRED,
            RulePriority.GLOBAL_PROHIBITION,
            source_name,
        )
        data["prohibited_behaviors"] = self._rules(
            data.get("prohibited_behaviors", []),
            RuleKind.PROHIBITED,
            RulePriority.GLOBAL_PROHIBITION,
            source_name,
        )
        data["escalation_rules"] = self._rules(
            data.get("escalation_rules", []),
            RuleKind.ESCALATION,
            RulePriority.GLOBAL_PROHIBITION,
            source_name,
        )
        data["contacts"] = self._contacts(data.get("contacts", {}), source_name)
        examples = data.pop("examples", {}) or {}
        if not isinstance(examples, Mapping) or set(examples) - {
            "positive",
            "negative",
        }:
            raise ValueError("examples accepts only positive and negative")
        data["examples_positive"] = tuple(
            str(item) for item in examples.get("positive", [])
        )
        data["examples_negative"] = tuple(
            str(item) for item in examples.get("negative", [])
        )
        return NormalizedRuleSource.model_validate(data)

    def _rules(
        self, values: object, kind: RuleKind, priority: RulePriority, source_name: str
    ) -> list[dict[str, Any]]:
        if not isinstance(values, (list, tuple)):
            raise TypeError(f"{kind.value} must be a list")
        result: list[dict[str, Any]] = []
        for index, value in enumerate(values):
            if isinstance(value, str):
                text = value.strip()
                item: dict[str, Any] = {"text": text}
            elif isinstance(value, Mapping):
                allowed = {
                    "rule_id",
                    "text",
                    "enforcement",
                    "priority",
                    "source_location",
                    "enabled",
                }
                unknown = set(value) - allowed
                if unknown:
                    raise ValueError(
                        f"unknown fields in {kind.value} rule: {sorted(unknown)}"
                    )
                item = dict(value)
                text = str(item.get("text", "")).strip()
            else:
                raise TypeError(f"{kind.value} rule must be string or mapping")
            if not text:
                raise ValueError(f"empty {kind.value} rule")
            item["text"] = text
            item["rule_id"] = item.get("rule_id") or f"{kind.value}-{index + 1}"
            item["kind"] = kind.value
            item["priority"] = item.get("priority", priority.value)
            item["source_location"] = item.get(
                "source_location", f"{source_name}:{kind.value}[{index}]"
            )
            item["enforcement"] = self._enforcement(item.get("enforcement"), kind, text)
            result.append(item)
        return result

    @staticmethod
    def _enforcement(value: object, kind: RuleKind, text: str) -> list[str]:
        if value is None:
            if kind == RuleKind.PROHIBITED:
                return [ExecutionMode.POLICY_GUARD.value]
            if kind == RuleKind.ESCALATION:
                return [ExecutionMode.MANUAL_ONLY.value]
            return [ExecutionMode.PROMPT_STYLE.value]
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, (list, tuple)):
            raise TypeError("enforcement must be a list")
        modes = [str(item) for item in value]
        if kind == RuleKind.PROHIBITED and not set(modes) & {
            mode.value
            for mode in (
                ExecutionMode.POLICY_GUARD,
                ExecutionMode.OUTPUT_VALIDATOR,
                ExecutionMode.MANUAL_ONLY,
            )
        }:
            raise ValueError("prohibited behavior cannot be prompt-only")
        return modes

    def _contacts(self, values: object, source_name: str) -> list[dict[str, Any]]:
        if values in (None, {}):
            return []
        if not isinstance(values, Mapping):
            raise TypeError("contacts must be a mapping keyed by stable contact_id")
        result: list[dict[str, Any]] = []
        for key, value in values.items():
            if not isinstance(value, Mapping):
                raise TypeError("contact override must be a mapping")
            if value.get("contact_id") != key:
                raise ValueError(
                    "contacts require an explicit stable contact_id matching the key"
                )
            allowed = {
                "contact_id",
                "required_behaviors",
                "prohibited_behaviors",
                "tone",
                "pacing",
            }
            unknown = set(value) - allowed
            if unknown:
                raise ValueError(f"unknown contact override fields: {sorted(unknown)}")
            item = dict(value)
            item["required_behaviors"] = self._rules(
                item.get("required_behaviors", []),
                RuleKind.REQUIRED,
                RulePriority.CONTACT,
                source_name,
            )
            item["prohibited_behaviors"] = self._rules(
                item.get("prohibited_behaviors", []),
                RuleKind.PROHIBITED,
                RulePriority.CONTACT,
                source_name,
            )
            result.append(item)
        return result

    def _report(self, source: NormalizedRuleSource) -> RulePackReport:
        rules = (
            list(source.required_behaviors)
            + list(source.prohibited_behaviors)
            + list(source.escalation_rules)
        )
        conflicts: list[ConflictReport] = []
        ambiguities: list[AmbiguityReport] = []
        for index, left in enumerate(rules):
            if _AMBIGUOUS.search(left.text):
                ambiguities.append(
                    AmbiguityReport(
                        ambiguity_id=f"amb-{left.rule_id}",
                        rule_id=left.rule_id,
                        excerpt=left.text,
                        reason="condition is not deterministically testable",
                        suggestion="define an observable threshold or require manual review",
                    )
                )
            for right in rules[index + 1 :]:
                exact_conflict = (
                    left.text.casefold() == right.text.casefold()
                    and left.kind != right.kind
                )
                broad_reply_conflict = (
                    left.kind == RuleKind.REQUIRED
                    and right.kind == RuleKind.PROHIBITED
                    and ("所有消息" in left.text or "必须回复" in left.text)
                    and ("敏感" in right.text or "禁止回复" in right.text)
                ) or (
                    right.kind == RuleKind.REQUIRED
                    and left.kind == RuleKind.PROHIBITED
                    and ("所有消息" in right.text or "必须回复" in right.text)
                    and ("敏感" in left.text or "禁止回复" in left.text)
                )
                if exact_conflict or broad_reply_conflict:
                    conflicts.append(
                        ConflictReport(
                            conflict_id=f"conf-{left.rule_id}-{right.rule_id}",
                            rule_ids=(left.rule_id, right.rule_id),
                            excerpts=(left.text, right.text),
                            priorities=(left.priority, right.priority),
                            suggestion="remove one rule or explicitly resolve precedence",
                        )
                    )
        tests: list[RuleTestCase] = []
        for rule in rules:
            kind = "negative" if rule.kind == RuleKind.PROHIBITED else "positive"
            tests.append(
                RuleTestCase(
                    test_id=f"test-{rule.rule_id}-1",
                    rule_id=rule.rule_id,
                    kind=kind,
                    input_text=rule.text,
                    expected="block"
                    if kind == "negative"
                    else "allow_with_constraints",
                    enforcement=rule.enforcement,
                )
            )
        valid = not conflicts and not ambiguities
        return RulePackReport(
            valid=valid,
            conflicts=tuple(conflicts),
            ambiguities=tuple(ambiguities),
            test_cases=tuple(tests),
        )


class SourceIngestor:
    def ingest(self, source: RuleSource) -> tuple[SourceFormat, object]:
        return extract(source)


class RuleNormalizer:
    def normalize(
        self, raw: object, *, source_name: str = "source"
    ) -> NormalizedRuleSource:
        return RulePackCompiler()._normalize(raw, source_name=source_name)


class EnforceabilityClassifier:
    def classify(self, text: str, kind: RuleKind) -> tuple[ExecutionMode, ...]:
        return tuple(RulePackCompiler()._enforcement(None, kind, text))


class PrecedenceResolver:
    _order: ClassVar[dict[RulePriority, int]] = {
        RulePriority.SYSTEM: 0,
        RulePriority.GLOBAL_PROHIBITION: 1,
        RulePriority.PLATFORM: 2,
        RulePriority.CONTACT: 3,
        RulePriority.TEMPORARY: 4,
        RulePriority.STYLE: 5,
    }

    def resolve(self, rules: tuple[Rule, ...] | list[Rule]) -> tuple[Rule, ...]:
        return tuple(sorted(rules, key=lambda rule: self._order[rule.priority]))


class ConflictAnalyzer:
    def analyze(self, source: NormalizedRuleSource) -> tuple[ConflictReport, ...]:
        return RulePackCompiler()._report(source).conflicts


class AmbiguityReporter:
    def analyze(self, source: NormalizedRuleSource) -> tuple[AmbiguityReport, ...]:
        return RulePackCompiler()._report(source).ambiguities


class TestCaseBuilder:
    def build(self, source: NormalizedRuleSource) -> tuple[RuleTestCase, ...]:
        return RulePackCompiler()._report(source).test_cases
