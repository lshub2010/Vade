"""`python -m vade.skill --check`: prints the catalog, starts nothing."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from .helpers import write_skill

ROOT = Path(__file__).resolve().parents[3]


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "core") + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "vade.skill", *args],
        cwd=str(cwd or ROOT / "core"),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def test_check_prints_catalog_and_exits_zero(tmp_path: Path) -> None:
    write_skill(tmp_path / "skills", "kb-guide", triggers=["知识库"])
    write_skill(tmp_path / "skills", "needs-tool", requires=["nope__tool"])
    result = _run("--check", "--dir", str(tmp_path / "skills"))
    assert result.returncode == 0, result.stderr
    assert "kb-guide" in result.stdout
    assert "needs-tool" in result.stdout
    assert "[unavailable]" in result.stdout


def test_check_json_is_machine_readable(tmp_path: Path) -> None:
    write_skill(tmp_path / "skills", "kb-guide")
    result = _run("--check", "--dir", str(tmp_path / "skills"), "--json")
    payload = json.loads(result.stdout)
    assert [row["name"] for row in payload["skills"]] == ["kb-guide"]
    assert payload["snapshot"]["apiVersion"] == "vade/skill-snapshot/v1"
    assert "SKILL.md" not in json.dumps(payload["skills"])


def test_strict_fails_on_an_invalid_skill(tmp_path: Path) -> None:
    write_skill(tmp_path / "skills", "kb-guide", sections={"When to Use": "only one"})
    result = _run("--check", "--dir", str(tmp_path / "skills"), "--strict")
    assert result.returncode == 1
    assert "missing section" in result.stdout
