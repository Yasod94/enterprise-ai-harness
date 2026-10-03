import json
import shutil
import subprocess
from pathlib import Path

import pytest

from change_harness.build import build
from change_harness.config import load

REPO = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def harness(tmp_path: Path) -> Path:
    """A copy of this harness repo (config + sources) with the KB URL filled in, built."""
    root = tmp_path / "harness"
    root.mkdir()
    shutil.copytree(REPO / "sources", root / "sources")
    text = (REPO / "harness.yaml").read_text().replace("url: <kb-mcp-url>", "url: https://kb.example.internal/mcp")
    (root / "harness.yaml").write_text(text)
    build(load(root))
    return root


@pytest.fixture
def service(tmp_path: Path) -> Path:
    """A service repo clone with one commit and no harness files."""
    repo = tmp_path / "svc"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "src").mkdir()
    (repo / "src/OrderPublisher.java").write_text("class OrderPublisher {}\n")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "init")
    return repo


def run_hook(harness: Path, repo: Path, mode: str, scope: str, event: dict | None = None, env: dict | None = None):
    script = harness / "plugin/scripts/harness-hook.cjs"
    payload = {"cwd": str(repo), "timestamp": "2026-10-02T00:00:00Z", **(event or {})}
    import os
    proc = subprocess.run(["node", str(script), mode, scope], input=json.dumps(payload), capture_output=True,
                          text=True, cwd=repo, timeout=60, env={**os.environ, **(env or {})})
    out = json.loads(proc.stdout) if proc.stdout.strip() else None
    return proc.returncode, out, proc.stderr


def start_run(repo: Path, run_id: str = "ABC-123") -> Path:
    run = repo / ".changes" / run_id
    run.mkdir(parents=True, exist_ok=True)
    (run / "state.json").write_text(json.dumps({"run": run_id, "source": "jira", "stage": "intake", "approvals": {}}))
    (repo / ".changes" / "current").write_text(run_id + "\n")
    return run


def make_index(repo: Path, rows: int = 50) -> Path:
    """A small SQLite database standing in for CodeGraph's index (WAL mode, like CodeGraph)."""
    db = repo / ".codegraph" / "codegraph.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    script = (
        "process.removeAllListeners('warning');"
        "const {DatabaseSync}=require('node:sqlite');"
        f"const d=new DatabaseSync({json.dumps(str(db))});"
        "d.exec('PRAGMA journal_mode=WAL; CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, hash TEXT)');"
        f"for(let i=0;i<{rows};i++) d.prepare('INSERT OR REPLACE INTO files VALUES (?,?)').run('src/F'+i+'.java','h'+i);"
        "d.close();"
    )
    subprocess.run(["node", "-e", script], check=True, capture_output=True)
    return db
