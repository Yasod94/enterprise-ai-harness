import json
import shutil
import subprocess
from pathlib import Path

import pytest

from change_harness.cli import main


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "svc"
    r.mkdir()
    git(r, "init", "-q")
    (r / "README.md").write_text("# svc\n")
    return r


@pytest.fixture
def initialized(repo: Path) -> Path:
    # Fill the KB placeholder so doctor has no errors by default.
    ext = repo / ".harness" / "extensions.yaml"
    ext.parent.mkdir(parents=True)
    ext.write_text("servers:\n  kb:\n    url: https://kb.example.internal/mcp\n")
    assert main(["init", "--repo", str(repo), "--source", "https://github.com/acme/change-harness"]) in (0, 1)
    return repo


NODE = shutil.which("node")


def run_hook(repo: Path, mode: str, scope: str, event: dict | None = None):
    script = repo / ".github/hooks/harness/harness-hook.cjs"
    payload = {"cwd": str(repo), "timestamp": "2026-10-02T00:00:00Z", **(event or {})}
    proc = subprocess.run(["node", str(script), mode, scope], input=json.dumps(payload),
                          capture_output=True, text=True, cwd=repo, timeout=20)
    out = json.loads(proc.stdout) if proc.stdout.strip() else None
    return proc.returncode, out, proc.stderr


def start_run(repo: Path, run_id: str = "ABC-123") -> Path:
    run = repo / ".changes" / run_id
    run.mkdir(parents=True, exist_ok=True)
    (run / "state.json").write_text(json.dumps({
        "run": run_id, "source": "jira", "stage": "intake", "approvals": {}, "usage": {},
    }))
    (repo / ".changes" / "current").write_text(run_id + "\n")
    return run
