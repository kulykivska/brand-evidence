"""Registry of projects: one .env (and therefore one database and store) per project."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

DEFAULT_REGISTRY = Path.home() / ".config" / "brand-evidence" / "projects.yaml"


class Project(BaseModel):
    env_file: Path
    workdir: Path | None = None


class Registry(BaseModel):
    projects: dict[str, Project] = Field(default_factory=dict)

    def get(self, name: str) -> Project:
        try:
            return self.projects[name]
        except KeyError:
            known = ", ".join(sorted(self.projects)) or "(none)"
            raise KeyError(f"unknown project {name!r}; known: {known}") from None


def load_registry(path: Path | None = None) -> Registry:
    path = path or DEFAULT_REGISTRY
    if not path.exists():
        return Registry()
    return Registry.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


def save_registry(registry: Registry, path: Path | None = None) -> Path:
    path = path or DEFAULT_REGISTRY
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "projects": {
            name: {
                "env_file": str(p.env_file),
                **({"workdir": str(p.workdir)} if p.workdir else {}),
            }
            for name, p in registry.projects.items()
        }
    }
    path.write_text(yaml.safe_dump(data, sort_keys=True), encoding="utf-8")
    return path
