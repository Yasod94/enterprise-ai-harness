import json
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone

import pytest

from enterprise_ai_harness.build import build
from enterprise_ai_harness.config import load
from conftest import NODE, git, make_index, run_hook, start_run

pytestmark = pytest.mark.skipif(NODE is None, reason="node is required for code graph tests")

FAKE_SYNC = "touch .codegraph/synced"


def cg(harness, repo, fn, idx=None, *args):
    """Call one function of codegraph-index.cjs and return its JSON-encoded result."""
    script = harness / "plugin/scripts/codegraph-index.cjs"
    call = ", ".join(json.dumps(a) for a in (str(repo), idx or {}, *args))
    code = f"const m=require({json.dumps(str(script))});console.log(JSON.stringify(m.{fn}({call})))"
    proc = subprocess.run(["node", "-e", code], capture_output=True, text=True, cwd=repo, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def count_rows(db):
    code = ("process.removeAllListeners('warning');const {DatabaseSync}=require('node:sqlite');"
            f"const d=new DatabaseSync({json.dumps(str(db))});"
            "console.log(d.prepare('SELECT count(*) AS n FROM files').get().n)")
    return int(subprocess.run(["node", "-e", code], capture_output=True, text=True, check=True).stdout)


def with_fake_sync(harness, refresh="stale"):
    text = (harness / "harness.yaml").read_text()
    text = text.replace("sync: npx -y @colbymchenry/codegraph@1.6.1 sync --quiet", f"sync: {FAKE_SYNC}")
    text = text.replace("seed_refresh: stale", f"seed_refresh: {refresh}")
    (harness / "harness.yaml").write_text(text)
    build(load(harness))


def test_save_then_restore_round_trip(harness, service):
    make_index(service, rows=40)
    note = cg(harness, service, "save", {}, "manual")
    assert "Code graph seed saved to .codegraph-seed/" in note
    meta = json.loads((service / ".codegraph-seed/SEED.json").read_text())
    assert meta["commit"] == git(service, "rev-parse", "HEAD") and meta["reason"] == "manual"
    assert meta["seed_bytes"] < meta["index_bytes"]
    assert not list((service / ".codegraph-seed").glob(".snapshot-*"))

    for f in (service / ".codegraph").iterdir():
        f.unlink()
    (service / ".codegraph").rmdir()
    restored = cg(harness, service, "restore")
    assert "restored from the committed seed" in restored
    assert count_rows(service / ".codegraph/codegraph.db") == 40
    assert cg(harness, service, "restore") is None  # an existing index is never overwritten


def test_save_reads_uncheckpointed_wal(harness, service):
    db = make_index(service, rows=5)
    # Hold a connection open with WAL writes pending, the way a running server would.
    holder = subprocess.Popen(["node", "-e", (
        "process.removeAllListeners('warning');const {DatabaseSync}=require('node:sqlite');"
        f"const d=new DatabaseSync({json.dumps(str(db))});d.exec('PRAGMA wal_autocheckpoint=0');"
        "for(let i=0;i<30;i++) d.prepare('INSERT INTO files VALUES (?,?)').run('src/W'+i+'.java','w');"
        "console.log('ready');setTimeout(()=>{},20000)")], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "ready"
        cg(harness, service, "save", {}, "manual")
    finally:
        holder.kill()
        holder.wait()
    restored = service / "restored.db"
    import gzip
    restored.write_bytes(gzip.decompress((service / ".codegraph-seed/codegraph.db.gz").read_bytes()))
    assert count_rows(restored) == 35


def test_save_without_index_fails_cleanly(harness, service):
    script = harness / "plugin/scripts/codegraph-index.cjs"
    proc = subprocess.run(["node", str(script), "save", str(service)], capture_output=True, text=True)
    assert proc.returncode == 1 and "build it first" in proc.stderr


def test_refresh_policy(harness, service):
    assert cg(harness, service, "shouldRefresh") is False  # no index yet
    make_index(service)
    assert cg(harness, service, "shouldRefresh") is True  # index but no seed
    cg(harness, service, "save", {}, "manual")
    assert cg(harness, service, "shouldRefresh") is False
    meta_path = service / ".codegraph-seed/SEED.json"
    meta = json.loads(meta_path.read_text())
    meta["built_at"] = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    meta_path.write_text(json.dumps(meta))
    assert cg(harness, service, "shouldRefresh") is True
    assert cg(harness, service, "shouldRefresh", {"seed_max_age_days": 30}) is False
    assert cg(harness, service, "shouldRefresh", {"seed_refresh": "manual"}) is False
    meta["built_at"] = datetime.now(timezone.utc).isoformat()
    meta_path.write_text(json.dumps(meta))
    assert cg(harness, service, "shouldRefresh", {"seed_refresh": "every-pr"}) is True


def wait_for(predicate, seconds=15):
    deadline = time.time() + seconds
    while not predicate() and time.time() < deadline:
        time.sleep(0.1)
    return predicate()


def test_background_sync_runs_one_loop_and_catches_up(harness, service):
    slow = "echo x >> .codegraph/rounds && sleep 1"
    assert cg(harness, service, "backgroundSync", {"sync": slow}) == "skipped"  # no index
    make_index(service)
    assert cg(harness, service, "backgroundSync", {}) == "skipped"  # no sync command
    assert cg(harness, service, "backgroundSync", {"sync": slow}) == "started"
    assert wait_for(lambda: (service / ".codegraph/rounds").exists())
    # An edit while the loop runs is queued, and the loop syncs once more before it exits.
    assert cg(harness, service, "backgroundSync", {"sync": slow}) == "queued"
    assert wait_for(lambda: not (service / ".codegraph/.harness-sync.pid").exists())
    assert (service / ".codegraph/rounds").read_text().count("x") == 2


def test_background_sync_defers_to_a_running_server(harness, service):
    make_index(service)
    (service / ".codegraph/daemon.pid").write_text(json.dumps({"pid": os.getpid(), "version": "1.6.1"}))
    assert cg(harness, service, "backgroundSync", {"sync": FAKE_SYNC}) == "watcher"
    (service / ".codegraph/daemon.pid").write_text("999999999\n")  # stale pid in the old plain format
    assert cg(harness, service, "backgroundSync", {"sync": FAKE_SYNC}) == "started"
    assert wait_for(lambda: (service / ".codegraph/synced").exists())


@pytest.mark.parametrize("content,message", [
    (b"version https://git-lfs.github.com/spec/v1\noid sha256:abc\n", "Git LFS pointer"),
    (b"not gzip at all", "not a valid gzip file"),
    (None, "does not contain a SQLite database"),
])
def test_restore_rejects_bad_seeds_once(harness, service, content, message):
    import gzip
    seed = service / ".codegraph-seed"
    seed.mkdir()
    (seed / "codegraph.db.gz").write_bytes(content if content is not None else gzip.compress(b"plain text"))
    first = cg(harness, service, "restore")
    assert message in first and not (service / ".codegraph/codegraph.db").exists()
    assert cg(harness, service, "restore") is None  # reported once per seed, not on every prompt


def test_save_leaves_no_temp_files_in_the_seed_folder(harness, service):
    make_index(service, rows=10)
    cg(harness, service, "save", {}, "manual")
    assert sorted(f.name for f in (service / ".codegraph-seed").iterdir()) == ["SEED.json", "codegraph.db.gz"]
    assert not list((service / ".codegraph").glob(".snapshot-*")) and not list((service / ".codegraph").glob(".seed-*"))


def test_hook_restores_seed_on_first_prompt(harness, service):
    make_index(service, rows=12)
    cg(harness, service, "save", {}, "manual")
    for f in (service / ".codegraph").iterdir():
        f.unlink()
    code, out, _ = run_hook(harness, service, "prompt", "orchestrator", {"prompt": "start ABC-123"})
    assert code == 0
    assert "restored from the committed seed" in out["hookSpecificOutput"]["additionalContext"]
    assert count_rows(service / ".codegraph/codegraph.db") == 12
    status = git(service, "status", "--porcelain", "--untracked-files=all")
    assert ".codegraph-seed/codegraph.db.gz" in status and ".codegraph/" not in status


def test_hook_syncs_after_code_edits_in_implement_only(harness, service):
    with_fake_sync(harness)
    make_index(service)
    start_run(service)
    run_hook(harness, service, "post-tool", "plan", {"tool_name": "create_file", "tool_response": "ok"})
    assert not (service / ".codegraph/synced").exists()
    run_hook(harness, service, "post-tool", "implement-task", {"tool_name": "read_file", "tool_response": "ok"})
    assert not (service / ".codegraph/.harness-sync-dirty").exists()
    run_hook(harness, service, "post-tool", "implement-task", {"tool_name": "replace_string_in_file", "tool_response": "ok"})
    assert wait_for(lambda: (service / ".codegraph/synced").exists())


def test_save_code_graph_command(harness, service):
    with_fake_sync(harness)
    make_index(service)
    code, out, _ = run_hook(harness, service, "prompt", "orchestrator", {"prompt": "save code graph"})
    assert code == 0 and "Code graph seed saved" in out["hookSpecificOutput"]["additionalContext"]
    assert (service / ".codegraph/synced").exists()  # synced before the snapshot
    assert json.loads((service / ".codegraph-seed/SEED.json").read_text())["reason"] == "requested"


def approve_all_but_verify(harness, service, run):
    for stage, files in (("intake", ["requirement.md"]), ("grill", ["clarifications.md"]),
                         ("impact", ["impact.md", "impact.json"]), ("plan", ["plan.md"]),
                         ("implement", ["progress.md"])):
        for name in files:
            (run / name).write_text("{}" if name.endswith(".json") else "content\n")
        code, _, err = run_hook(harness, service, "prompt", stage, {"prompt": f"[harness:approve {stage}] skip cross-check"})
        assert code == 0, err
    (run / "verification.md").write_text("content\n")


@pytest.mark.parametrize("refresh,expect_save", [("stale", True), ("manual", False)])
def test_verify_approval_refreshes_seed_by_policy(harness, service, refresh, expect_save):
    with_fake_sync(harness, refresh)
    make_index(service)
    run = start_run(service)
    approve_all_but_verify(harness, service, run)
    code, out, err = run_hook(harness, service, "prompt", "verify", {"prompt": "[harness:approve verify]"})
    assert code == 0, err
    context = out["hookSpecificOutput"]["additionalContext"]
    assert ("Refresh code graph seed" in context) is expect_save
    assert (service / ".codegraph-seed/codegraph.db.gz").exists() is expect_save
    if expect_save:
        assert json.loads((service / ".codegraph-seed/SEED.json").read_text())["reason"] == "pr ABC-123"
