"""launchd plists for macOS. Each job invokes the CLI, which writes a runs row before working."""

from __future__ import annotations

import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

LABEL_PREFIX = "dev.brand-evidence"
AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"

JOBS: dict[str, dict] = {  # type: ignore[type-arg]
    "crawl": {"StartCalendarInterval": {"Hour": 7, "Minute": 0}},
    "archive-poll": {"StartInterval": 2 * 60 * 60},
    "digest": {"StartCalendarInterval": {"Hour": 7, "Minute": 30}},
    "timestamp": {"StartCalendarInterval": {"Hour": 7, "Minute": 45}},
}


def _executable() -> str:
    exe = shutil.which("brand-evidence")
    if exe:
        return exe
    return str(Path(sys.executable).with_name("brand-evidence"))


def label_for(job: str, project: str | None) -> str:
    return f"{LABEL_PREFIX}.{project}.{job}" if project else f"{LABEL_PREFIX}.{job}"


def plist_for(job: str, project_dir: Path, log_dir: Path, project: str | None = None) -> dict:  # type: ignore[type-arg]
    args = [_executable()] + (["--project", project] if project else []) + [job]
    data = {
        "Label": label_for(job, project),
        "ProgramArguments": args,
        "WorkingDirectory": str(project_dir),
        "StandardOutPath": str(log_dir / f"{job}.out.log"),
        "StandardErrorPath": str(log_dir / f"{job}.err.log"),
        "RunAtLoad": False,
        "EnvironmentVariables": {"PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"},
    }
    data.update(JOBS[job])
    return data


def install(project_dir: Path, *, project: str | None = None, dry_run: bool = False) -> list[Path]:
    log_dir = project_dir / "logs"
    written: list[Path] = []
    if not dry_run:
        log_dir.mkdir(exist_ok=True)
        AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    for job in JOBS:
        path = AGENTS_DIR / f"{label_for(job, project)}.plist"
        if dry_run:
            written.append(path)
            continue
        if path.exists():
            subprocess.run(["launchctl", "unload", str(path)], check=False, capture_output=True)  # noqa: S603,S607
        path.write_bytes(plistlib.dumps(plist_for(job, project_dir, log_dir, project)))
        subprocess.run(["launchctl", "load", str(path)], check=True)  # noqa: S603,S607
        written.append(path)
    return written


def uninstall() -> list[Path]:
    removed: list[Path] = []
    for path in sorted(AGENTS_DIR.glob(f"{LABEL_PREFIX}.*.plist")):
        subprocess.run(["launchctl", "unload", str(path)], check=False, capture_output=True)  # noqa: S603,S607
        path.unlink()
        removed.append(path)
    return removed
