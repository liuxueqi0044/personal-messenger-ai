"""Format extraction with no network or code execution."""

from __future__ import annotations

import re
import zipfile
from io import BytesIO
from xml.etree import ElementTree

import yaml

from .models import RuleSource, SourceFormat


class SourceParseError(ValueError):
    pass


def detect_format(name: str, explicit: SourceFormat | None = None) -> SourceFormat:
    if explicit:
        return explicit
    suffix = name.lower().rsplit(".", 1)[-1] if "." in name else "txt"
    return {
        "md": SourceFormat.MARKDOWN,
        "markdown": SourceFormat.MARKDOWN,
        "txt": SourceFormat.TEXT,
        "yaml": SourceFormat.YAML,
        "yml": SourceFormat.YAML,
        "docx": SourceFormat.DOCX,
    }.get(suffix, SourceFormat.TEXT)


def extract(source: RuleSource) -> tuple[SourceFormat, object]:
    fmt = detect_format(source.name, source.format)
    if fmt == SourceFormat.YAML:
        try:
            value = yaml.safe_load(source.content.decode("utf-8"))
        except (UnicodeDecodeError, yaml.YAMLError) as exc:
            raise SourceParseError(f"invalid YAML: {exc}") from exc
        if not isinstance(value, dict):
            raise SourceParseError("YAML root must be a mapping")
        return fmt, value
    if fmt == SourceFormat.DOCX:
        return fmt, extract_docx_text(source.content)
    try:
        return fmt, source.content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SourceParseError("text source must be UTF-8") from exc


def extract_docx_text(content: bytes) -> str:
    try:
        with zipfile.ZipFile(BytesIO(content)) as archive:
            xml = archive.read("word/document.xml")
    except (KeyError, zipfile.BadZipFile) as exc:
        raise SourceParseError("invalid DOCX: missing word/document.xml") from exc
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise SourceParseError("invalid DOCX XML") from exc
    paragraphs: list[str] = []
    for paragraph in root.iter(
        "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"
    ):
        text = "".join(
            node.text or ""
            for node in paragraph.iter(
                "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"
            )
        ).strip()
        if text:
            paragraphs.append(text)
    return "\n".join(paragraphs)


_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*(.+?)\s*$")
_BULLET = re.compile(r"^\s*[-*+]\s+(.+?)\s*$")


def markdown_to_mapping(text: str) -> dict[str, object]:
    """Map a small, explicit Markdown/TXT convention to the strict schema."""
    if text.lstrip().startswith("---"):
        parts = text.lstrip().split("---", 2)
        if len(parts) == 3:
            try:
                front_matter = yaml.safe_load(parts[1])
            except yaml.YAMLError as exc:
                raise SourceParseError(f"invalid Markdown front matter: {exc}") from exc
            if isinstance(front_matter, dict):
                return front_matter
    sections: dict[str, list[str]] = {
        "required_behaviors": [],
        "prohibited_behaviors": [],
        "escalation_rules": [],
        "positive": [],
        "negative": [],
    }
    current = "identity"
    identity_lines: list[str] = []
    for line in text.splitlines():
        heading = _HEADING.match(line)
        if heading:
            name = heading.group(1).casefold()
            if any(token in name for token in ("禁止", "prohibit", "不得")):
                current = "prohibited_behaviors"
            elif any(token in name for token in ("必须", "required", "行为")):
                current = "required_behaviors"
            elif any(token in name for token in ("升级", "人工", "escalat")):
                current = "escalation_rules"
            elif any(token in name for token in ("正例", "positive")):
                current = "positive"
            elif any(token in name for token in ("反例", "negative")):
                current = "negative"
            else:
                current = "identity"
            continue
        bare_heading = line.strip().casefold()
        if bare_heading in {"必须行为", "required behaviors", "requirements"}:
            current = "required_behaviors"
            continue
        if bare_heading in {
            "禁止事项",
            "禁止行为",
            "prohibited behaviors",
            "prohibitions",
        }:
            current = "prohibited_behaviors"
            continue
        if bare_heading in {"升级规则", "人工审核", "escalation rules"}:
            current = "escalation_rules"
            continue
        bullet = _BULLET.match(line)
        value = bullet.group(1).strip() if bullet else line.strip()
        if not value:
            continue
        if current == "identity" and not bullet:
            if value.startswith(("必须", "要求", "应该")):
                current = "required_behaviors"
            elif value.startswith(("禁止", "不得", "不允许", "不要")):
                current = "prohibited_behaviors"
            else:
                identity_lines.append(value)
                continue
        if current == "identity":
            identity_lines.append(value)
        else:
            sections[current].append(value)
    return {
        "schema_version": 1,
        "rulepack_id": "personal-default",
        "persona": {
            "identity": " ".join(identity_lines) or "待用户文件补充",
            "language": "zh-CN",
            "tone": [],
            "preferred_length": "concise",
        },
        "required_behaviors": sections["required_behaviors"],
        "prohibited_behaviors": sections["prohibited_behaviors"],
        "escalation_rules": sections["escalation_rules"],
        "pacing": {},
        "contacts": {},
        "examples": {
            "positive": sections["positive"],
            "negative": sections["negative"],
        },
    }
