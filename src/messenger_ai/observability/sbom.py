"""Offline CycloneDX-like JSON inventory for direct project dependencies."""

from __future__ import annotations

import importlib.metadata
import re
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _name(requirement: str) -> str:
    match = re.match(r"\s*([A-Za-z0-9_.-]+)", requirement)
    return match.group(1) if match else requirement


def _license(distribution: importlib.metadata.Distribution) -> str:
    metadata = distribution.metadata
    expression = metadata.get("License-Expression")
    if expression:
        return expression.strip()
    declared = metadata.get("License")
    if declared and declared.strip() and declared.strip().casefold() != "unknown":
        return declared.strip()[:500]
    classifiers = metadata.get_all("Classifier") or []
    licenses = [item.split(" :: ")[-1] for item in classifiers if "License ::" in item]
    return ", ".join(licenses) if licenses else "unknown"


def generate_sbom(
    project_root: str | Path,
    *,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    root = Path(project_root)
    with (root / "pyproject.toml").open("rb") as handle:
        document = tomllib.load(handle)
    project = document["project"]
    requirements: dict[tuple[str, str], str] = {}
    for requirement in project.get("dependencies", []):
        requirements[(_name(requirement).casefold(), "runtime")] = requirement
    for scope, items in project.get("optional-dependencies", {}).items():
        for requirement in items:
            requirements[(_name(requirement).casefold(), f"optional:{scope}")] = (
                requirement
            )

    components = []
    for (normalized_name, scope), requirement in sorted(requirements.items()):
        package_name = _name(requirement)
        try:
            distribution = importlib.metadata.distribution(package_name)
            installed_version = distribution.version
            license_name = _license(distribution)
        except importlib.metadata.PackageNotFoundError:
            installed_version = "unknown"
            license_name = "unknown"
        components.append(
            {
                "type": "library",
                "name": package_name,
                "normalized_name": normalized_name,
                "declared_requirement": requirement,
                "installed_version": installed_version,
                "license": license_name,
                "scope": scope,
            }
        )
    timestamp = generated_at or datetime.now(UTC)
    return {
        "bomFormat": "PMAI-JSON-SBOM",
        "specVersion": "1.0",
        "serialNumber": f"urn:uuid:{project['name']}-{project['version']}",
        "generatedAt": timestamp.astimezone(UTC).isoformat(),
        "metadata": {
            "component": {
                "type": "application",
                "name": project["name"],
                "version": project["version"],
            },
            "networkAccessUsed": False,
        },
        "components": components,
    }
