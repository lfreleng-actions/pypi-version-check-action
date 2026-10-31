# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Regression cover for the pip steps in action.yaml.

The end-to-end workflow job runs on images that already ship pip, so
it never reaches the ensurepip fallback (issue #150). The first tests
run the action's own step scripts on a PATH without any pip command,
under the flags GitHub uses for ``shell: bash``, with a stub
interpreter in place of Python. The stub records every call, reports
pip missing until ensurepip "installs" it, and answers ``pip index``
queries.

The later tests use a real interpreter. The steps run in a workspace
standing in for the caller's checkout, holding modules named like the
ones pip and ensurepip import; each records that it was imported. They
check that none loads, with and without an empty PYTHONPATH entry,
that a workspace-relative pip setting still applies, and, through a
control run of the previous commands, that the checks can fail.

The scripts are read out of ``action.yaml`` rather than copied here, so
the tests cannot drift from the implementation they cover.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import venv
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
RUN_PIP = REPO_ROOT / "scripts" / "run_pip.py"
TRUSTED_PYTHONPATH = REPO_ROOT / "scripts" / "trusted_pythonpath.py"
PIP_STEP = "Check pip installed/available"
INDEX_STEP = "Check Python package index"

# GitHub runs "shell: bash" steps as: bash --noprofile --norc -eo pipefail
BASH_FLAGS = ["--noprofile", "--norc", "-eo", "pipefail"]

# External commands the step scripts need; "which" is included so the
# pre-fix script fails for its own reasons, not a missing command.
HOST_TOOLS = ("grep", "sed", "tr", "which")

# Installed as both "python" and "python3". Logs each call, one per
# line, then emulates run_pip.py and the ensurepip module. A marker
# file makes an ensurepip install visible to later calls.
STUB = """#!{bash}
printf '%s %s\\n' "${{0##*/}}" "$*" >> "$STUB_LOG"
case "$*" in
  "-I -S "*"/trusted_pythonpath.py")
    exec "$STUB_REAL_PYTHON" "$@" ;;
  *"/run_pip.py --version")
    [ -e "$STUB_PIP_MARKER" ] || exit 1
    echo "pip 99.0 (stub)" ;;
  "-m ensurepip --upgrade")
    printf 'ensurepip userbase=%s\\n' "${{PYTHONUSERBASE-<unset>}}" >> "$STUB_LOG"
    printf 'ensurepip home=%s\\n' "${{HOME-<unset>}}" >> "$STUB_LOG"
    [ "$STUB_ENSUREPIP" = "ok" ] || {{ echo "ensurepip: failed" >&2; exit 1; }}
    : > "$STUB_PIP_MARKER"
    echo "Successfully installed pip-99.0" ;;
  *"/run_pip.py index "*)
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


def _step(name: str) -> dict[str, object]:
    """Return the named step, read from action.yaml."""
    action = cast(
        "dict[str, dict[str, list[dict[str, object]]]]",
        yaml.safe_load((REPO_ROOT / "action.yaml").read_text()),
    )
    for step in action["runs"]["steps"]:
        if step.get("name") == name:
            return step
    raise AssertionError(f"step not found in action.yaml: {name}")


def _expand(
    value: str,
    workspace: Path,
    runner_temp: Path | None = None,
    outputs: dict[str, str] | None = None,
) -> str:
    """Substitute the expressions the action's steps use."""
    value = value.replace("${{ github.action_path }}", str(REPO_ROOT)).replace(
        "${{ github.workspace }}", str(workspace)
    )
    if runner_temp is not None:
        value = value.replace("${{ runner.temp }}", str(runner_temp))
    if "${{ steps.pip.outputs.python }}" in value:
        python = (outputs or {}).get("python")
        assert python, "run the pip check step first: it chooses the interpreter"
        value = value.replace("${{ steps.pip.outputs.python }}", python)
    return value


def _step_env(
    name: str,
    workspace: Path,
    runner_temp: Path | None = None,
    outputs: dict[str, str] | None = None,
) -> dict[str, str]:
    """The step's own env, except the inputs, which each test supplies."""
    env = cast("dict[str, str]", _step(name).get("env") or {})
    return {
        key: _expand(str(value), workspace, runner_temp, outputs)
        for key, value in env.items()
        if not key.startswith("INPUT_")
    }


def _step_directory(name: str, workspace: Path) -> Path:
    """Where the runner runs the step: its working-directory, else the workspace."""
    directory = str(_step(name).get("working-directory") or "")
    return Path(_expand(directory, workspace)) if directory else workspace


def _base_env(path: str) -> dict[str, str]:
    """The test's environment, minus anything that changes how Python or pip run."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PYTHON", "PIP_", "VIRTUAL_ENV"))
    }
    env["PATH"] = path
    return env


def _make_runner(tmp_path: Path, python: Path | None) -> StepRunner:
    """Run step scripts on a pip-free PATH, as the runner does.

    ``python`` is the interpreter installed as python/python3; with
    None, the stub stands in for it.
    """
    bash = shutil.which("bash")
    assert bash, "bash is required to run the step scripts"

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in HOST_TOOLS:
        target = shutil.which(tool)
        assert target, f"host tool not found: {tool}"
        (bin_dir / tool).symlink_to(target)
    stub = bin_dir / "python3"
    if python is None:
        _ = stub.write_text(STUB.format(bash=bash))
    else:
        # A wrapper, not a symlink: Python finds its venv from its own path.
        _ = stub.write_text(f'#!/bin/sh\nexec "{python}" "$@"\n')
    stub.chmod(0o755)
    (bin_dir / "python").symlink_to("python3")

    path = str(bin_dir)
    assert shutil.which("pip", path=path) is None, "pip must be absent"
    assert shutil.which("pip3", path=path) is None, "pip3 must be absent"

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    log = tmp_path / "invocations.log"
    log.touch()
    github_output = tmp_path / "github_output"
    github_output.touch()

    def run(
        step: str,
        *,
        ensurepip: str = "ok",
        extra_env: dict[str, str] | None = None,
        directory: Path | None = None,
        **inputs: str,
    ) -> StepResult:
        script = tmp_path / "step.sh"
        _ = script.write_text(str(_step(step)["run"]))
        outputs = dict(
            line.split("=", 1)
            for line in github_output.read_text().splitlines()
            if "=" in line
        )
        env = _base_env(path)
        env.update(_step_env(step, workspace, runner_temp, outputs))
        env.update(
            STUB_LOG=str(log),
            STUB_PIP_MARKER=str(tmp_path / "pip-installed"),
            STUB_ENSUREPIP=ensurepip,
            STUB_REAL_PYTHON=sys.executable,
            GITHUB_OUTPUT=str(github_output),
            GITHUB_WORKSPACE=str(workspace),
            RUNNER_TEMP=str(runner_temp),
            PLANTED_MARKER=str(tmp_path / "planted-ran"),
        )
        env.update(extra_env or {})
        env.update({f"INPUT_{key.upper()}": value for key, value in inputs.items()})
        proc = subprocess.run(
            [bash, *BASH_FLAGS, str(script)],
            capture_output=True,
            text=True,
            env=env,
            cwd=directory or _step_directory(step, workspace),
            check=False,
        )
        return StepResult(proc, log.read_text(), github_output.read_text().splitlines())

    return run


@pytest.fixture(name="run_step")
def _run_step(tmp_path: Path) -> StepRunner:
    """Run a step script on a pip-free PATH with a stub interpreter."""
    return _make_runner(tmp_path, None)


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
    assert f"python3 {RUN_PIP} index" in result.calls, result.calls
    assert "package_match=true" in result.outputs, result.outputs
    assert "version_match=true" in result.outputs, result.outputs


# Each planted file records that it was imported, then stops the run.
PLANTED_SOURCE = """\
import os
import sys

with open(os.environ["PLANTED_MARKER"], "a", encoding="utf-8") as marker:
    marker.write(__name__ + "\\n")
sys.exit(97)
"""

# Named like the modules the steps run with -m, a module pip imports,
# and sitecustomize, which Python imports at start-up from an empty
# PYTHONPATH entry.
PLANTED = (
    "pip/__init__.py",
    "pip/__main__.py",
    "ensurepip/__init__.py",
    "ensurepip/__main__.py",
    "json.py",
    "sitecustomize.py",
)

# How a caller can put the working directory on sys.path: -m puts it
# first; an empty PYTHONPATH entry adds it in any mode.
PYTHONPATHS = pytest.mark.parametrize(
    "pythonpath", [None, ":"], ids=["no-pythonpath", "empty-pythonpath-entry"]
)


def _write_index(root: Path, versions: tuple[str, ...]) -> str:
    """A local simple index offering ITR at the given versions."""
    project = root / "itr"
    project.mkdir(parents=True)
    links = "".join(
        f'<a href="itr-{version}.tar.gz">itr-{version}.tar.gz</a>\n'
        for version in versions
    )
    _ = (project / "index.html").write_text(f"<html><body>\n{links}</body></html>\n")
    return root.as_uri()


@pytest.fixture(name="real_python", scope="module")
def _real_python(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A virtual environment with pip, standing in for the runner's Python."""
    env_dir = tmp_path_factory.mktemp("python")
    venv.create(env_dir, with_pip=True)
    return env_dir / "bin" / "python3"


@pytest.fixture(name="index_url", scope="module")
def _index_url(tmp_path_factory: pytest.TempPathFactory) -> str:
    """ITR 1.1.10 and 1.1.9, so no network is used."""
    return _write_index(tmp_path_factory.mktemp("index"), ("1.1.10", "1.1.9"))


def _plant(workspace: Path) -> None:
    for name in PLANTED:
        target = workspace / name
        target.parent.mkdir(parents=True, exist_ok=True)
        _ = target.write_text(PLANTED_SOURCE)


@pytest.fixture(name="run_real")
def _run_real(tmp_path: Path, real_python: Path) -> StepRunner:
    """Run step scripts with a real interpreter and planted workspace modules."""
    run = _make_runner(tmp_path, real_python)
    _plant(tmp_path / "workspace")
    return run


def _planted_imports(tmp_path: Path) -> str:
    marker = tmp_path / "planted-ran"
    return marker.read_text() if marker.exists() else ""


def _control(
    tmp_path: Path, python: Path, args: list[str], pythonpath: str | None
) -> str:
    """Run a Python command in the workspace and return what it imported from there."""
    env = _base_env(os.defpath)
    env["PLANTED_MARKER"] = str(tmp_path / "planted-ran")
    if pythonpath is not None:
        env["PYTHONPATH"] = pythonpath
    _ = subprocess.run(
        [str(python), *args],
        cwd=tmp_path / "workspace",
        env=env,
        capture_output=True,
        check=False,
    )
    return _planted_imports(tmp_path)


def _control_args(pythonpath: str | None, *pip_args: str) -> list[str]:
    """The pip command a step would run without this change.

    'python3 -m pip' before it; with an empty PYTHONPATH entry,
    run_pip.py alone, which the step's 'unset PYTHONPATH' then covers.
    """
    if pythonpath is None:
        return ["-m", "pip", *pip_args]
    return [str(RUN_PIP), *pip_args]


@PYTHONPATHS
def test_pip_check_ignores_workspace_modules(
    run_real: StepRunner, real_python: Path, tmp_path: Path, pythonpath: str | None
) -> None:
    """The pip check runs the installed pip, never one in the workspace."""
    extra = {} if pythonpath is None else {"PYTHONPATH": pythonpath}
    result = run_real(PIP_STEP, extra_env=extra)

    assert _planted_imports(tmp_path) == "", result.proc.stdout + result.proc.stderr
    assert result.proc.returncode == 0, result.proc.stdout + result.proc.stderr
    assert "Successfully installed pip" not in result.proc.stdout

    control = _control(
        tmp_path, real_python, _control_args(pythonpath, "--version"), pythonpath
    )
    assert control != "", "the control run imported nothing from the workspace"


@PYTHONPATHS
def test_index_query_ignores_workspace_modules(
    run_real: StepRunner,
    real_python: Path,
    tmp_path: Path,
    index_url: str,
    pythonpath: str | None,
) -> None:
    """The index query runs the installed pip and reports the real versions."""
    extra = {} if pythonpath is None else {"PYTHONPATH": pythonpath}
    setup = run_real(PIP_STEP, extra_env=extra)
    assert setup.proc.returncode == 0, setup.proc.stdout + setup.proc.stderr
    result = run_real(
        INDEX_STEP,
        extra_env=extra,
        index_url=index_url,
        package_name="ITR",
        package_version="1.1.10",
        pre_release="false",
        exit_on_fail="true",
    )

    assert _planted_imports(tmp_path) == "", result.proc.stdout + result.proc.stderr
    assert result.proc.returncode == 0, result.proc.stdout + result.proc.stderr
    assert "package_match=true" in result.outputs, result.outputs
    assert "version_match=true" in result.outputs, result.outputs

    args = _control_args(pythonpath, "index", f"--index-url={index_url}", "versions")
    control = _control(tmp_path, real_python, [*args, "ITR"], pythonpath)
    assert control != "", "the control run imported nothing from the workspace"


def test_ensurepip_ignores_workspace_modules(tmp_path: Path) -> None:
    """With pip missing, ensurepip installs it and imports nothing from the workspace.

    ensurepip runs pip in a second interpreter, started with -c in the
    same working directory, so a planted json.py (which pip imports)
    would load there if ensurepip ran in the workspace.
    """
    env_dir = tmp_path / "python-without-pip"
    venv.create(env_dir, with_pip=False)
    python = env_dir / "bin" / "python3"
    run = _make_runner(tmp_path, python)
    _plant(tmp_path / "workspace")

    result = run(PIP_STEP)

    assert _planted_imports(tmp_path) == "", result.proc.stdout + result.proc.stderr
    assert result.proc.returncode == 0, result.proc.stdout + result.proc.stderr
    assert "Successfully installed pip ✅" in result.proc.stdout, result.proc.stdout
    again = run(PIP_STEP)
    assert again.proc.returncode == 0, again.proc.stdout + again.proc.stderr
    assert "Successfully installed pip" not in again.proc.stdout, "pip not installed"

    control = _control(tmp_path, python, ["-m", "ensurepip", "--upgrade"], None)
    assert control != "", "the control run imported nothing from the workspace"


def test_workspace_relative_pip_config_still_applies(
    run_real: StepRunner, tmp_path: Path, index_url: str
) -> None:
    """A caller's PIP_CONFIG_FILE relative to the workspace is still read.

    The config adds an extra index offering ITR 2.0.0, which the main
    index lacks, so version_match is true only when pip reads it. A
    control run from another directory, where the relative path finds
    nothing, reports false.
    """
    extra_index = _write_index(tmp_path / "extra-index", ("2.0.0",))
    workspace = tmp_path / "workspace"
    _ = (workspace / "pip.conf").write_text(
        f"[global]\nextra-index-url = {extra_index}\n"
    )
    inputs = {
        "index_url": index_url,
        "package_name": "ITR",
        "package_version": "2.0.0",
        "pre_release": "false",
        "exit_on_fail": "false",
    }
    setup = run_real(PIP_STEP)
    assert setup.proc.returncode == 0, setup.proc.stdout + setup.proc.stderr

    result = run_real(INDEX_STEP, extra_env={"PIP_CONFIG_FILE": "pip.conf"}, **inputs)

    assert result.proc.returncode == 0, result.proc.stdout + result.proc.stderr
    assert "version_match=true" in result.outputs, (
        result.proc.stdout + result.proc.stderr
    )
    assert _planted_imports(tmp_path) == ""

    control = run_real(
        INDEX_STEP,
        extra_env={"PIP_CONFIG_FILE": "pip.conf"},
        directory=REPO_ROOT,
        **inputs,
    )
    assert "version_match=false" in control.outputs, control.outputs


def _run_filter(
    python: str,
    cwd: Path,
    workspace: Path,
    pythonpath: str | None,
    **extra: str,
) -> subprocess.CompletedProcess[str]:
    """Run trusted_pythonpath.py as the steps do, with -I -S."""
    env = _base_env(os.defpath)
    env["GITHUB_WORKSPACE"] = str(workspace)
    if pythonpath is not None:
        env["PYTHONPATH"] = pythonpath
    env.update(extra)
    return subprocess.run(
        [python, "-I", "-S", str(TRUSTED_PYTHONPATH)],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _settings(output: str) -> dict[str, str | None]:
    """Apply the filter's shell output in bash; return the settings it makes."""
    bash = shutil.which("bash")
    assert bash
    names = ("PYTHONPATH", "PYTHONNOUSERSITE", "PYTHONUSERBASE")
    script = 'PYTHONPATH=unchanged; eval "$1"; ' + "; ".join(
        f'printf "%s\\n" "${{{name}-<unset>}}"' for name in names
    )
    done = subprocess.run(
        [bash, "-c", script, "bash", output],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": os.defpath},
    )
    values = done.stdout.split("\n")[: len(names)]
    return {
        name: None if value == "<unset>" else value
        for name, value in zip(names, values)
    }


def _filter(pythonpath: str, cwd: Path, workspace: Path) -> str | None:
    """The PYTHONPATH the steps keep, or None when they unset it."""
    done = _run_filter(sys.executable, cwd, workspace, pythonpath)
    assert done.returncode == 0, done.stderr
    return _settings(done.stdout)["PYTHONPATH"]


def test_pythonpath_filter_keeps_only_entries_outside_the_workspace(
    tmp_path: Path,
) -> None:
    """Empty, relative, in-workspace and parent entries go; other absolute ones stay."""
    workspace = tmp_path / "workspace"
    (workspace / "lib").mkdir(parents=True)
    outside = tmp_path / "site-target"
    outside.mkdir()
    (tmp_path / "link-into-workspace").symlink_to(workspace / "lib")
    (tmp_path / "link-to-parent").symlink_to(tmp_path)
    entries = [
        "",
        ".",
        "lib",
        str(workspace),
        str(workspace / "lib"),
        str(tmp_path / "link-into-workspace"),
        str(tmp_path),
        str(tmp_path) + "/",
        str(tmp_path / "link-to-parent"),
        "/",
        str(outside),
        "/opt/tools/site-packages",
        "",
    ]

    kept = _filter(os.pathsep.join(entries), workspace, workspace)

    assert kept == os.pathsep.join([str(outside), "/opt/tools/site-packages"])
    assert _filter("", workspace, workspace) is None
    assert _filter(":", workspace, workspace) is None


def test_pythonpath_filter_output_is_quoted(tmp_path: Path) -> None:
    """An entry holding shell syntax comes back as the same text."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    odd = f"/opt/a b/$(touch {tmp_path}/ran)/`id`/'q'"

    assert _filter(odd, workspace, workspace) == odd
    assert not (tmp_path / "ran").exists()


def test_user_site_inside_the_workspace_is_replaced(tmp_path: Path) -> None:
    """A user site in the workspace moves to the fallback base, or turns off.

    With FALLBACK_USER_BASE set (absolute, outside the workspace) the
    filter points PYTHONUSERBASE there, so pip can still fall back to a
    user install; without one, or with one inside the workspace, it
    turns the user site off. A user site elsewhere is left alone.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fallback = str(tmp_path / "runner-temp" / "user-base")
    cases = {
        str(workspace / "user"): (fallback, None),
        "rel-user": (fallback, None),
        str(tmp_path / "elsewhere"): (None, None),
    }
    for user_base, (moved, off) in cases.items():
        done = _run_filter(
            sys.executable,
            workspace,
            workspace,
            None,
            PYTHONUSERBASE=user_base,
            FALLBACK_USER_BASE=fallback,
        )
        assert done.returncode == 0, done.stderr
        settings = _settings(done.stdout)
        # The filter leaves a user base elsewhere alone: it prints nothing
        assert settings["PYTHONUSERBASE"] == moved, user_base
        assert settings["PYTHONNOUSERSITE"] == off, user_base
    # With PYTHONUSERBASE unset, the user site is under HOME
    done = _run_filter(
        sys.executable,
        workspace,
        workspace,
        None,
        HOME=str(workspace),
        FALLBACK_USER_BASE=fallback,
    )
    assert _settings(done.stdout)["PYTHONUSERBASE"] == fallback
    # No usable fallback: the user site is turned off instead
    for unusable in ("", "relative/base", str(workspace / "base"), str(tmp_path)):
        done = _run_filter(
            sys.executable,
            workspace,
            workspace,
            None,
            PYTHONUSERBASE=str(workspace / "user"),
            FALLBACK_USER_BASE=unusable,
        )
        assert _settings(done.stdout)["PYTHONNOUSERSITE"] == "1", unusable


def test_pythonpath_filter_runs_no_pth_files(tmp_path: Path) -> None:
    """With -S, a .pth file printing ':' cannot add an empty entry.

    -I alone still processes .pth files in the interpreter's own
    site-packages; the steps capture what the filter prints, so such a
    file's output would become part of PYTHONPATH.
    """
    env_dir = tmp_path / "python"
    venv.create(env_dir, with_pip=False)
    python = env_dir / "bin" / "python3"
    purelib = subprocess.run(
        [
            str(python),
            "-c",
            "import sysconfig; print(sysconfig.get_paths()['purelib'])",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    _ = (Path(purelib) / "zz-prints.pth").write_text(
        "import sys; sys.stdout.write(':')\n"
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    done = _run_filter(str(python), workspace, workspace, "/opt/x")

    assert done.returncode == 0, done.stderr
    assert _settings(done.stdout)["PYTHONPATH"] == "/opt/x", done.stdout
    control = subprocess.run(
        [str(python), "-I", "-c", "pass"], capture_output=True, text=True, check=True
    )
    assert control.stdout.startswith(":"), "the .pth file did not run without -S"


def test_pip_found_through_pythonpath_outside_the_workspace(
    tmp_path: Path, real_python: Path
) -> None:
    """A pip exposed by an absolute PYTHONPATH entry is still found.

    The interpreter has no pip of its own, so the pip check passes only
    through the --target directory on PYTHONPATH; the ':' entries
    around it, which name the workspace, are dropped.
    """
    target = tmp_path / "pip-target"
    _ = subprocess.run(
        [
            str(real_python),
            "-m",
            "pip",
            "install",
            "--quiet",
            "--no-index",
            "--target",
            str(target),
            "--find-links",
            str(_bundled_wheels()),
            "pip",
        ],
        check=True,
        env=_base_env(os.defpath),
    )
    bare_dir = tmp_path / "python-without-pip"
    venv.create(bare_dir, with_pip=False)
    run = _make_runner(tmp_path, bare_dir / "bin" / "python3")
    _plant(tmp_path / "workspace")

    result = run(
        PIP_STEP,
        ensurepip="unused",
        extra_env={"PYTHONPATH": f":{target}:"},
    )

    assert result.proc.returncode == 0, result.proc.stdout + result.proc.stderr
    assert "Successfully installed pip" not in result.proc.stdout
    assert _planted_imports(tmp_path) == ""


def _bundled_wheels() -> Path:
    """The directory holding ensurepip's bundled pip wheel."""
    import ensurepip

    bundled = Path(ensurepip.__file__).parent / "_bundled"
    assert any(bundled.glob("pip-*.whl")), f"no bundled pip wheel in {bundled}"
    return bundled


def test_python_inside_the_workspace_is_never_run(
    tmp_path: Path, real_python: Path, index_url: str
) -> None:
    """A python3 from a PATH entry in the workspace is skipped, unrun.

    PATH starts with entries that resolve into the workspace: an
    absolute .venv/bin there, the same directory relative, and a
    directory outside that is a symlink into it. Each holds a python3
    that records it ran. The steps must choose the next python3 on PATH
    and run none of those, in the pip check or the index query.
    """
    run = _make_runner(tmp_path, real_python)
    workspace = tmp_path / "workspace"
    venv_bin = workspace / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    marker = tmp_path / "planted-ran"
    planted = venv_bin / "python3"
    _ = planted.write_text(f"#!/bin/sh\necho workspace-python >> '{marker}'\nexit 1\n")
    planted.chmod(0o755)
    (tmp_path / "link-into-workspace").symlink_to(venv_bin)
    path = os.pathsep.join(
        [
            str(venv_bin),
            ".venv/bin",
            str(tmp_path / "link-into-workspace"),
            str(tmp_path / "bin"),
        ]
    )

    check = run(PIP_STEP, extra_env={"PATH": path})
    query = run(
        INDEX_STEP,
        extra_env={"PATH": path},
        index_url=index_url,
        package_name="ITR",
        package_version="1.1.10",
        pre_release="false",
        exit_on_fail="true",
    )

    assert not marker.exists(), marker.read_text()
    assert check.proc.returncode == 0, check.proc.stdout + check.proc.stderr
    assert f"python={tmp_path / 'bin' / 'python3'}" in check.outputs, check.outputs
    assert query.proc.returncode == 0, query.proc.stdout + query.proc.stderr
    assert "version_match=true" in query.outputs, query.outputs


def test_no_python_outside_the_workspace_fails_clearly(tmp_path: Path) -> None:
    """With every python3 on PATH in the workspace, the step stops and says so."""
    run = _make_runner(tmp_path, tmp_path / "unused-python")
    venv_bin = tmp_path / "workspace" / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    marker = tmp_path / "planted-ran"
    planted = venv_bin / "python3"
    _ = planted.write_text(f"#!/bin/sh\necho workspace-python >> '{marker}'\n")
    planted.chmod(0o755)
    tools = tmp_path / "tools"
    tools.mkdir()
    for tool in HOST_TOOLS:
        target = shutil.which(tool)
        assert target
        (tools / tool).symlink_to(target)

    result = run(PIP_STEP, extra_env={"PATH": f"{venv_bin}{os.pathsep}{tools}"})

    assert result.proc.returncode == 1, result.proc.stdout + result.proc.stderr
    assert "No python3 found on PATH outside the workspace" in result.proc.stdout
    assert not marker.exists()


def test_pythonpath_above_the_workspace_imports_nothing(
    tmp_path: Path, real_python: Path
) -> None:
    """A PYTHONPATH entry above the workspace cannot import the checkout.

    The checkout's directory is named pip and holds an __init__.py that
    records it ran. PYTHONPATH names its parent, so 'pip' would resolve
    to the checkout; the step drops that entry. A control run of
    run_pip.py with the entry kept imports the checkout.
    """
    run = _make_runner(tmp_path, real_python)
    parent = tmp_path / "checkouts"
    checkout = parent / "pip"
    checkout.mkdir(parents=True)
    marker = tmp_path / "planted-ran"
    _ = (checkout / "__init__.py").write_text(PLANTED_SOURCE)
    env = {"PYTHONPATH": str(parent), "GITHUB_WORKSPACE": str(checkout)}

    result = run(PIP_STEP, extra_env=env, directory=checkout)

    assert result.proc.returncode == 0, result.proc.stdout + result.proc.stderr
    assert not marker.exists(), marker.read_text()
    control_env = _base_env(os.defpath)
    control_env.update(PYTHONPATH=str(parent), PLANTED_MARKER=str(marker))
    _ = subprocess.run(
        [str(real_python), str(RUN_PIP), "--version"],
        cwd=checkout,
        env=control_env,
        capture_output=True,
        check=False,
    )
    assert marker.exists(), "the control run did not import the checkout"


@pytest.mark.parametrize(
    ("variable", "logged"), [("PYTHONUSERBASE", "userbase"), ("HOME", "home")]
)
def test_relative_user_dirs_keep_their_meaning(
    run_step: StepRunner, tmp_path: Path, variable: str, logged: str
) -> None:
    """A relative PYTHONUSERBASE or HOME still names the same place for ensurepip.

    ensurepip runs from the action's directory, so the step makes the
    value absolute first; pip falling back to a user install then puts
    pip where the later index step, run in the workspace, looks for it.
    The value points outside the workspace, so the filter keeps it.
    """
    extra = {variable: f"../{logged}-dir"}
    if variable == "HOME":
        extra["PYTHONUSERBASE"] = ""
    result = run_step(PIP_STEP, extra_env=extra)

    assert result.proc.returncode == 0, result.proc.stdout + result.proc.stderr
    prefix = f"ensurepip {logged}="
    seen = [
        line[len(prefix) :]
        for line in result.calls.splitlines()
        if line.startswith(prefix)
    ]
    assert seen, result.calls
    assert os.path.isabs(seen[0]), seen
    assert os.path.realpath(seen[0]) == os.path.realpath(tmp_path / f"{logged}-dir")


def test_workspace_user_site_moves_to_the_fallback(
    run_step: StepRunner, tmp_path: Path
) -> None:
    """ensurepip installs into the shared fallback base, not the workspace."""
    workspace_base = tmp_path / "workspace" / "user-base"
    result = run_step(PIP_STEP, extra_env={"PYTHONUSERBASE": str(workspace_base)})

    assert result.proc.returncode == 0, result.proc.stdout + result.proc.stderr
    fallback = tmp_path / "runner-temp" / "pypi-version-check-action" / "user-base"
    assert f"ensurepip userbase={fallback}" in result.calls, result.calls


def test_pythonpath_filter_has_no_annotations() -> None:
    """The filter runs on any Python 3, so it cannot use annotations.

    'list[str]' raises TypeError when Python 3.8 or older defines the
    function, and 'from __future__ import annotations' would import a
    module the steps keep out of the workspace's reach.
    """
    import ast

    tree = ast.parse(TRUSTED_PYTHONPATH.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            arguments = [*node.args.args, *node.args.kwonlyargs]
            assert node.returns is None, node.name
            assert all(arg.annotation is None for arg in arguments), node.name
        assert not isinstance(node, ast.ImportFrom), "no __future__ or from-imports"


@pytest.mark.parametrize("version", ["3.6", "3.7", "3.8"])
def test_pythonpath_filter_runs_on_old_pythons(tmp_path: Path, version: str) -> None:
    """Where an old interpreter is installed, the filter runs on it."""
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("needs uv to find an interpreter")
    assert uv is not None
    env = _base_env(os.defpath)
    env["UV_NO_CONFIG"] = "1"
    found = subprocess.run(
        [uv, "python", "find", "--system", "--no-project", version],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        check=False,
    )
    if found.returncode != 0:
        pytest.skip(f"Python {version} is not installed")
    env.update(PYTHONPATH=f":{tmp_path}/lib:/opt/x", GITHUB_WORKSPACE=str(tmp_path))
    done = subprocess.run(
        [found.stdout.strip(), "-I", "-S", str(TRUSTED_PYTHONPATH)],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert _settings(done.stdout)["PYTHONPATH"] == "/opt/x"


@pytest.fixture(name="system_python", scope="module")
def _system_python(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A non-venv CPython of its own, as a runner has; it reads the user site.

    Installed by uv into this test's temporary directory, so no
    interpreter on the host is used or changed.
    """
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("needs uv to install a private interpreter")
    assert uv is not None
    root = tmp_path_factory.mktemp("system-python")
    env = _base_env(os.defpath)
    env.update(UV_NO_CONFIG="1", UV_PYTHON_INSTALL_DIR=str(root))
    for command in (
        ["install", "3.12"],
        ["find", "--managed-python", "--no-project", "3.12"],
    ):
        done = subprocess.run(
            [uv, "python", *command],
            capture_output=True,
            text=True,
            env=env,
            cwd=root,
            check=False,
        )
        if done.returncode != 0:
            pytest.skip(f"uv could not provide Python 3.12: {done.stderr.strip()}")
    python = Path(done.stdout.strip())
    assert root in python.resolve().parents, python
    return python


def test_user_site_in_the_workspace_runs_nothing(
    tmp_path: Path, system_python: Path, index_url: str
) -> None:
    """A .pth file in a workspace user site does not run in either step.

    PYTHONUSERBASE names a directory in the workspace whose user site
    holds a .pth file recording that it ran. A control run of the same
    interpreter with that setting shows the file runs when the user site
    is on.
    """
    workspace = tmp_path / "workspace"
    run = _make_runner(tmp_path, system_python)
    _plant(workspace)
    user_base = workspace / "user-base"
    version = subprocess.run(
        [str(system_python), "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    user_site = user_base / "lib" / f"python{version}" / "site-packages"
    user_site.mkdir(parents=True)
    marker = tmp_path / "planted-ran"
    _ = (user_site / "planted.pth").write_text(
        f"import sys; open({str(marker)!r}, 'a').write('user-site .pth\\n')\n"
    )
    env = {"PYTHONUSERBASE": str(user_base)}

    check = run(PIP_STEP, extra_env=env)
    query = run(
        INDEX_STEP,
        extra_env=env,
        index_url=index_url,
        package_name="ITR",
        package_version="1.1.10",
        pre_release="false",
        exit_on_fail="true",
    )

    assert _planted_imports(tmp_path) == "", check.proc.stderr + query.proc.stderr
    assert check.proc.returncode == 0, check.proc.stdout + check.proc.stderr
    assert query.proc.returncode == 0, query.proc.stdout + query.proc.stderr
    assert "version_match=true" in query.outputs, query.outputs

    control = _base_env(os.defpath)
    control["PYTHONUSERBASE"] = str(user_base)
    _ = subprocess.run([str(system_python), "-c", "pass"], env=control, check=True)
    assert _planted_imports(tmp_path) == "user-site .pth\n"


def _set_writable(tree: Path, root: Path, writable: bool) -> None:
    """Add or drop write permission on ``tree``, which must lie under ``root``."""
    assert root in tree.resolve().parents, f"refusing to change {tree}"
    for path in [tree, *tree.rglob("*")]:
        if path.is_symlink():
            continue
        mode = path.stat().st_mode
        path.chmod(mode | stat.S_IWUSR if writable else mode & ~0o222)


@pytest.fixture(name="bare_system_python", scope="module")
def _bare_system_python(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[Path]:
    """A non-venv CPython with no pip and a read-only site-packages.

    As on a runner whose system site-packages the job user cannot write:
    pip can only be installed into the user site. Built from a private
    uv-managed install in this test's temporary directory; its bundled
    pip and its EXTERNALLY-MANAGED marker are removed there, and nothing
    on the host is changed.
    """
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("needs uv to install a private interpreter")
    assert uv is not None
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("read-only site-packages does not stop root")
    root = tmp_path_factory.mktemp("bare-python").resolve()
    env = _base_env(os.defpath)
    env.update(UV_NO_CONFIG="1", UV_PYTHON_INSTALL_DIR=str(root))
    for command in (
        ["install", "3.12"],
        ["find", "--managed-python", "--no-project", "3.12"],
    ):
        done = subprocess.run(
            [uv, "python", *command],
            capture_output=True,
            text=True,
            env=env,
            cwd=root,
            check=False,
        )
        if done.returncode != 0:
            pytest.skip(f"uv could not provide Python 3.12: {done.stderr.strip()}")
    python = Path(done.stdout.strip())
    assert root in python.resolve().parents, python
    paths = cast(
        "dict[str, str]",
        json.loads(
            subprocess.run(
                [
                    str(python),
                    "-I",
                    "-c",
                    "import json, sysconfig; print(json.dumps(sysconfig.get_paths()))",
                ],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        ),
    )
    purelib = Path(paths["purelib"]).resolve()
    assert root in purelib.parents, purelib
    for entry in purelib.iterdir():
        if entry.name == "pip" or entry.name.startswith("pip-"):
            shutil.rmtree(entry)
    marker = Path(paths["stdlib"]).resolve() / "EXTERNALLY-MANAGED"
    if marker.exists():
        assert root in marker.parents
        marker.unlink()
    _set_writable(purelib, root, writable=False)
    try:
        yield python
    finally:
        _set_writable(purelib, root, writable=True)


def test_read_only_python_falls_back_to_the_shared_user_base(
    tmp_path: Path, bare_system_python: Path, index_url: str
) -> None:
    """With HOME in the workspace, pip still installs, outside the workspace.

    The interpreter has no pip and cannot write its site-packages, so
    ensurepip must fall back to a user install. HOME is in the
    workspace, so the user site there is replaced by the fallback base
    under RUNNER_TEMP: pip is installed there, and the index query finds
    it there. A control run without a fallback base turns the user site
    off, and the install fails.
    """
    run = _make_runner(tmp_path, bare_system_python)
    home = tmp_path / "workspace" / "home"
    home.mkdir()
    env = {"HOME": str(home)}

    check = run(PIP_STEP, extra_env=env)
    query = run(
        INDEX_STEP,
        extra_env=env,
        index_url=index_url,
        package_name="ITR",
        package_version="1.1.10",
        pre_release="false",
        exit_on_fail="true",
    )

    assert check.proc.returncode == 0, check.proc.stdout + check.proc.stderr
    assert "Successfully installed pip ✅" in check.proc.stdout, check.proc.stdout
    fallback = tmp_path / "runner-temp" / "pypi-version-check-action" / "user-base"
    assert list(fallback.rglob("pip/__init__.py")), "pip not in the fallback base"
    assert not list(home.rglob("pip/__init__.py")), "pip installed in the workspace"
    assert query.proc.returncode == 0, query.proc.stdout + query.proc.stderr
    assert "version_match=true" in query.outputs, query.outputs

    shutil.rmtree(fallback)
    control = run(PIP_STEP, extra_env={**env, "FALLBACK_USER_BASE": ""})
    assert control.proc.returncode == 1, control.proc.stdout + control.proc.stderr
    assert "Unable to find/install pip command" in control.proc.stdout
