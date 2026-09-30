# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Regression cover for the pip fallback in action.yaml (issue #150).

The end-to-end workflow job runs on images that already ship pip, so
it never reaches the ensurepip fallback. These tests run the action's
own step scripts on a PATH without any pip command, under the flags
GitHub uses for ``shell: bash``, with a stub interpreter in place of
Python. The stub records every call, reports pip missing until
ensurepip "installs" it, and answers ``pip index`` queries.

The scripts are read out of ``action.yaml`` rather than copied here, so
the tests cannot drift from the implementation they cover.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
PIP_STEP = "Check pip installed/available"
INDEX_STEP = "Check Python package index"

# GitHub runs "shell: bash" steps as: bash --noprofile --norc -eo pipefail
BASH_FLAGS = ["--noprofile", "--norc", "-eo", "pipefail"]

# External commands the step scripts need; "which" is included so the
# pre-fix script fails for its own reasons, not a missing command.
HOST_TOOLS = ("grep", "sed", "tr", "which")

# Installed as both "python" and "python3". Logs each call, one per
# line, then emulates the pip and ensurepip modules. A marker file
# makes an ensurepip install visible to later calls.
STUB = """#!{bash}
printf '%s %s\\n' "${{0##*/}}" "$*" >> "$STUB_LOG"
case "$*" in
  "-m pip --version")
    [ -e "$STUB_PIP_MARKER" ] || exit 1
    echo "pip 99.0 (stub)" ;;
  "-m ensurepip --upgrade")
    [ "$STUB_ENSUREPIP" = "ok" ] || {{ echo "ensurepip: failed" >&2; exit 1; }}
    : > "$STUB_PIP_MARKER"
    echo "Successfully installed pip-99.0" ;;
  "-m pip index "*)
    [ -e "$STUB_PIP_MARKER" ] || exit 1
    echo "ITR (1.1.10)"
    echo "Available versions: 1.1.10, 1.1.9" ;;
  *)
    exit 1 ;;
esac
"""


@dataclass
class StepResult:
    """Outcome of one step run, plus the state it shares with others."""

    proc: subprocess.CompletedProcess[str]
    calls: str
    outputs: list[str]


StepRunner = Callable[..., StepResult]


def _step_script(name: str) -> str:
    """Return the run body of the named step, read from action.yaml."""
    action = cast(
        "dict[str, dict[str, list[dict[str, str]]]]",
        yaml.safe_load((REPO_ROOT / "action.yaml").read_text()),
    )
    for step in action["runs"]["steps"]:
        if step.get("name") == name:
            return step["run"]
    raise AssertionError(f"step not found in action.yaml: {name}")


@pytest.fixture(name="run_step")
def _run_step(tmp_path: Path) -> StepRunner:
    """Run a step script on a pip-free PATH with a stub interpreter."""
    bash = shutil.which("bash")
    assert bash, "bash is required to run the step scripts"

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in HOST_TOOLS:
        target = shutil.which(tool)
        assert target, f"host tool not found: {tool}"
        (bin_dir / tool).symlink_to(target)
    stub = bin_dir / "python3"
    _ = stub.write_text(STUB.format(bash=bash))
    stub.chmod(0o755)
    (bin_dir / "python").symlink_to("python3")

    path = str(bin_dir)
    assert shutil.which("pip", path=path) is None, "pip must be absent"
    assert shutil.which("pip3", path=path) is None, "pip3 must be absent"

    log = tmp_path / "invocations.log"
    log.touch()
    github_output = tmp_path / "github_output"
    github_output.touch()

    def run(step: str, *, ensurepip: str = "ok", **inputs: str) -> StepResult:
        script = tmp_path / "step.sh"
        _ = script.write_text(_step_script(step))
        env = dict(os.environ)
        env.update(
            PATH=path,
            STUB_LOG=str(log),
            STUB_PIP_MARKER=str(tmp_path / "pip-installed"),
            STUB_ENSUREPIP=ensurepip,
            GITHUB_OUTPUT=str(github_output),
        )
        env.update({f"INPUT_{key.upper()}": value for key, value in inputs.items()})
        proc = subprocess.run(
            [bash, *BASH_FLAGS, str(script)],
            capture_output=True,
            text=True,
            env=env,
            cwd=tmp_path,
            check=False,
        )
        return StepResult(proc, log.read_text(), github_output.read_text().splitlines())

    return run


def test_missing_pip_is_installed_with_ensurepip(run_step: StepRunner) -> None:
    """With no pip available, the step installs it and succeeds."""
    result = run_step(PIP_STEP)

    assert result.proc.returncode == 0, result.proc.stdout + result.proc.stderr
    assert "python3 -m ensurepip --upgrade" in result.calls, result.calls
    assert "Successfully installed pip ✅" in result.proc.stdout, result.proc.stdout


def test_ensurepip_failure_reports_error(run_step: StepRunner) -> None:
    """When ensurepip cannot install pip, the step says so and fails."""
    result = run_step(PIP_STEP, ensurepip="fail")

    assert result.proc.returncode == 1, result.proc.stdout + result.proc.stderr
    assert "python3 -m ensurepip --upgrade" in result.calls, result.calls
    assert "Unable to find/install pip command ❌" in result.proc.stdout, (
        result.proc.stdout
    )


def test_index_query_uses_the_checked_pip(run_step: StepRunner) -> None:
    """The index query runs the pip the previous step checked/installed.

    ensurepip installs the pip module but does not guarantee a ``pip``
    command on PATH, so a bare ``pip index`` call finds nothing.
    """
    setup = run_step(PIP_STEP)
    assert setup.proc.returncode == 0, setup.proc.stdout + setup.proc.stderr

    result = run_step(
        INDEX_STEP,
        index_url="https://example.invalid/simple",
        package_name="ITR",
        package_version="1.1.10",
        pre_release="false",
        exit_on_fail="true",
    )

    assert result.proc.returncode == 0, result.proc.stdout + result.proc.stderr
    assert "python3 -m pip index" in result.calls, result.calls
    assert "package_match=true" in result.outputs, result.outputs
    assert "version_match=true" in result.outputs, result.outputs
