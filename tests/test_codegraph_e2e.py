"""End-to-end check against the real CodeGraph CLI.

Runs only when CODEGRAPH_E2E=1. Set CODEGRAPH_BIN to a local codegraph binary,
or leave it unset to use the npx command pinned in harness.yaml.
"""

import os
import shutil
import subprocess

import pytest

from change_harness.build import build
from change_harness.config import load
from conftest import NODE, git, run_hook

pytestmark = pytest.mark.skipif(NODE is None or os.environ.get("CODEGRAPH_E2E") != "1",
                                reason="set CODEGRAPH_E2E=1 to run against the real CodeGraph CLI")

PINNED = "npx -y @colbymchenry/codegraph@1.6.1"


def codegraph_cmd():
    return os.environ.get("CODEGRAPH_BIN") or PINNED


def run_cg(repo, *args, env):
    cmd = f"{codegraph_cmd()} {' '.join(args)}"
    return subprocess.run(cmd, shell=True, cwd=repo, capture_output=True, text=True, timeout=600, env=env)


def test_seed_round_trip_with_real_codegraph(harness, service, tmp_path):
    if os.environ.get("CODEGRAPH_BIN"):
        text = (harness / "harness.yaml").read_text().replace(PINNED, os.environ["CODEGRAPH_BIN"])
        (harness / "harness.yaml").write_text(text)
        build(load(harness))
    env = {**os.environ, "DO_NOT_TRACK": "1", "HOME": str(tmp_path / "home")}
    (tmp_path / "home").mkdir()
    src = service / "src"
    (src / "Pricing.java").write_text("class Pricing { double total(int q) { return q * 2.0; } }\n")
    (src / "OrderPublisher.java").write_text(
        "class OrderPublisher { void publish(int q) { new Pricing().total(q); } }\n")
    git(service, "add", "-A")
    git(service, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "code")

    assert run_cg(service, "init", "--yes", env=env).returncode == 0
    code, out, _ = run_hook(harness, service, "prompt", "orchestrator", {"prompt": "save code graph"}, env=env)
    assert "Code graph seed saved" in out["hookSpecificOutput"]["additionalContext"]
    git(service, "add", ".codegraph-seed")
    git(service, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "seed")

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(service), str(clone)], check=True)
    code, out, _ = run_hook(harness, clone, "prompt", "orchestrator", {"prompt": "hello"}, env=env)
    assert "restored from the committed seed" in out["hookSpecificOutput"]["additionalContext"]
    status = run_cg(clone, "status", env=env)
    assert status.returncode == 0 and "up to date" in status.stdout.lower(), status.stdout + status.stderr

    (clone / "src/Refunds.java").write_text("class Refunds { double refund(int q) { return 0; } }\n")
    assert run_cg(clone, "sync", "--quiet", env=env).returncode == 0
    assert "refund" in run_cg(clone, "query", "Refunds", env=env).stdout
    porcelain = git(clone, "status", "--porcelain", "--untracked-files=all")
    assert ".codegraph/" not in porcelain and porcelain.strip() == "?? src/Refunds.java"
    shutil.rmtree(tmp_path / "home", ignore_errors=True)
