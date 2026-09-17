"""Enforces arch-rules.yaml so the rules are checked, not just documented."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
RULES = yaml.safe_load((ROOT / "arch-rules.yaml").read_text())["rules"]


def _files(scope: list[str]) -> list[Path]:
    out: list[Path] = []
    for entry in scope:
        p = ROOT / entry
        if p.is_file():
            out.append(p)
        elif p.is_dir():
            out.extend(
                f
                for f in p.rglob("*")
                if f.is_file()
                and f.suffix in {".py", ".md", ".yaml", ".yml", ".j2", ".feature", ".example"}
            )
    return out


def test_arch_rules() -> None:
    violations: list[str] = []
    for rule in RULES:
        files = [f for f in _files(rule["scope"]) if f.name != "test_arch_rules.py"]
        for f in files:
            text = f.read_text(encoding="utf-8", errors="ignore")
            for mod in rule.get("forbidden_imports", []):
                if re.search(rf"^\s*(import|from)\s+{re.escape(mod)}\b", text, re.M):
                    violations.append(f"{rule['id']}: {f.relative_to(ROOT)} imports {mod}")
            for pat in rule.get("forbidden_patterns", []):
                if pat in text:
                    violations.append(f"{rule['id']}: {f.relative_to(ROOT)} contains {pat!r}")
            if rule.get("forbidden_regex") and re.search(rule["forbidden_regex"], text):
                violations.append(f"{rule['id']}: {f.relative_to(ROOT)} matches regex")
    assert not violations, "\n".join(violations)
