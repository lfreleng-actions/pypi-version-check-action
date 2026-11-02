# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for scripts/check_index.py and the action steps that run it.

The end-to-end cases run the script as the action does, ``python -I``,
against a local HTTP index serving PEP 691 JSON and PEP 503 HTML
pages, so no network is needed. The step cases run the scripts read
out of ``action.yaml``, so they cannot drift from the implementation.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast, final

import check_index as ci
import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "check_index.py"
JSON_TYPE = "application/vnd.pypi.simple.v1+json"
BASH = ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c"]
ITR_FILES = [
    "ITR-1.1.9.tar.gz",
    "itr-1.1.10-py3-none-any.whl",
    "itr-1.1.10.tar.gz",
    "itr-2.0.0rc1-py3-none-any.whl",
    "itr-2.0.0.dev3.tar.gz",
    "README.txt",
]


def json_page(files: list[str]) -> bytes:
    """A PEP 691 project page listing the files."""
    entries: list[dict[str, object]] = [
        {"filename": name, "url": name, "hashes": {}} for name in files
    ]
    return json.dumps(
        {"meta": {"api-version": "1.1"}, "name": "x", "files": entries}
    ).encode()


def html_page(files: list[str]) -> bytes:
    """A PEP 503 project page listing the files."""
    links = "".join(
        f'<a href="../../files/{name}#sha256=00">{name}</a><br>' for name in files
    )
    return ("<!DOCTYPE html><html><body>" + links + "</body></html>").encode()


class IndexServer(ThreadingHTTPServer):
    """A local simple index that records each request's path."""

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), IndexHandler)
        self.paths: list[str] = []


@final
class IndexHandler(BaseHTTPRequestHandler):
    """Serves the simple index; the request path picks the behaviour."""

    def send(
        self,
        code: int,
        body: bytes = b"",
        content_type: str = "text/plain",
        location: str = "",
    ) -> None:
        """Send one complete response."""
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if location:
            self.send_header("Location", location)
        self.end_headers()
        _ = self.wfile.write(body)

    def do_GET(self) -> None:
        """Answer one GET."""
        cast("IndexServer", self.server).paths.append(self.path)
        if self.path == "/private/itr/":
            expected = "Basic " + base64.b64encode(b"user:p@ss").decode()
            if self.headers.get("Authorization") == expected:
                self.send(200, json_page(ITR_FILES), JSON_TYPE)
            else:
                self.send(401)
        elif self.path == "/simple/itr/":
            self.send(200, json_page(ITR_FILES), JSON_TYPE)
        elif self.path == "/html/itr/":
            self.send(200, html_page(ITR_FILES), "text/html; charset=utf-8")
        elif self.path == "/simple/empty/":
            self.send(200, json_page([]), JSON_TYPE)
        elif self.path == "/simple/broken/":
            self.send(500)
        elif self.path == "/simple/badjson/":
            self.send(200, b"{", JSON_TYPE)
        elif self.path == "/simple/plain/":
            self.send(200, b"x", "text/plain")
        elif self.path == "/simple/tofile/":
            self.send(302, location="file:///etc/passwd")
        elif self.path == "/moved/itr/":
            self.send(301, location="/simple/itr/")
        elif self.path == "/simple/short/":
            self.send_cut(b"Content-Length: 4096", json_page(ITR_FILES))
        elif self.path == "/simple/chunked/":
            self.send_cut(
                b"Transfer-Encoding: chunked", b"400\r\n" + json_page(ITR_FILES)
            )
        elif self.path == "/simple/cuthtml/":
            self.send(200, html_page(ITR_FILES)[:-40], "text/html")
        elif self.path == "/simple/charset/":
            self.send(200, html_page(ITR_FILES), "text/html; charset=no-such-codec")
        else:
            self.send(404)

    def send_cut(self, framing: bytes, body: bytes) -> None:
        """Promise more body than is sent, then close the connection."""
        head = b"HTTP/1.1 200 OK\r\nContent-Type: " + JSON_TYPE.encode()
        _ = self.wfile.write(head + b"\r\n" + framing + b"\r\n\r\n" + body)
        self.close_connection = True


@pytest.fixture(name="index", scope="module")
def _index() -> Iterator[IndexServer]:
    server = IndexServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()


def base(server: IndexServer, path: str = "simple") -> str:
    """The index URL the server answers for under ``path``."""
    host, port = server.server_address[:2]
    return f"http://{host!s}:{port}/{path}"


@dataclass
class Run:
    """One run of the script: its status, its log and its outputs."""

    code: int
    log: str
    outputs: dict[str, str]


def run_script(tmp_path: Path, inputs: dict[str, str], cwd: Path = REPO_ROOT) -> Run:
    """Run the script the way the action's check step does."""
    output = tmp_path / "github_output"
    output.touch()
    env = {"PATH": os.environ["PATH"], "GITHUB_OUTPUT": str(output)}
    env.update({f"INPUT_{key.upper()}": value for key, value in inputs.items()})
    proc = subprocess.run(
        [sys.executable, "-I", str(SCRIPT)],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    lines = output.read_text().splitlines()
    outputs = {key: value for key, _, value in (line.partition("=") for line in lines)}
    return Run(proc.returncode, proc.stdout + proc.stderr, outputs)


@pytest.mark.parametrize(
    ("raw", "text", "key", "pre"),
    [
        ("1.1.10", "1.1.10", "1.1.10", False),
        ("v1.1.10", "1.1.10", "1.1.10", False),
        ("1.0.0", "1.0.0", "1", False),
        ("01.002", "1.2", "1.2", False),
        ("1.0.0RC1", "1.0.0rc1", "1rc1", True),
        ("1.0-alpha.2", "1.0a2", "1a2", True),
        ("1.0c1", "1.0rc1", "1rc1", True),
        ("1.0-1", "1.0.post1", "1.post1", False),
        ("1.0.rev", "1.0.post0", "1.post0", False),
        ("0.1.22.dev1", "0.1.22.dev1", "0.1.22.dev1", True),
        ("1!2.0", "1!2.0", "1!2", False),
        ("0!2.0", "2.0", "2", False),
        ("1.0+Ubuntu-01.x", "1.0+ubuntu.1.x", "1+ubuntu.1.x", False),
    ],
)
def test_parse_version_normalises(raw: str, text: str, key: str, pre: bool) -> None:
    """Versions normalise as PEP 440 defines."""
    assert ci.parse_version(raw) == ci.Version(text=text, key=key, is_prerelease=pre)


@pytest.mark.parametrize(
    "raw", ["", "latest", "1.0.0::x", "1..0", "##[error]1.0", "1.0\n::stop"]
)
def test_parse_version_rejects(raw: str) -> None:
    """Text that is not a PEP 440 version parses to nothing."""
    assert ci.parse_version(raw) is None


@pytest.mark.parametrize(
    ("filename", "project", "version"),
    [
        ("itr-1.1.10-py3-none-any.whl", "itr", "1.1.10"),
        ("ITR-1.1.10-1-py3-none-any.whl", "itr", "1.1.10"),
        ("My.Project-2.0.tar.gz", "my-project", "2.0"),
        ("my_project-2.0.ZIP", "my-project", "2.0"),
        ("my-project-2.0-1.tar.gz", "my-project", "2.0-1"),
        ("other-1.0.tar.gz", "itr", None),
        ("other-1.0-py3-none-any.whl", "itr", None),
        ("my_project-2.0.egg", "my-project", None),
        ("my_project-2.0-py3-any.whl", "my-project", None),
    ],
)
def test_version_from_filename(
    filename: str, project: str, version: str | None
) -> None:
    """Wheels and sdists of the project give their version; other files none."""
    assert ci.version_from_filename(filename, project) == version


def test_project_url_moves_credentials_to_a_header() -> None:
    """Credentials leave the URL and become a Basic header value."""
    url, credentials = ci.project_url(
        "https://user:p%40ss@nexus.example/repo/simple/", "itr"
    )
    assert url == "https://nexus.example/repo/simple/itr/"
    assert base64.b64decode(credentials or "") == b"user:p@ss"


@pytest.mark.parametrize(
    "url", ["file:///etc", "ftp://x/simple", "simple", "https:///simple"]
)
def test_project_url_rejects_other_schemes(url: str) -> None:
    """Only http and https index URLs with a host are accepted."""
    with pytest.raises(ci.QueryError):
        _ = ci.project_url(url, "itr")


@pytest.mark.parametrize(
    ("index_url", "expected"),
    [
        (
            "https://user:secret@host.example/a::b/##[error]x/",
            "https://host.example/a%3A%3Ab/itr/",
        ),
        ("http://[::1]:8080/simple", "http://[::1]:8080/simple/itr/"),
        (
            "https://Nexus.Example.:443/repo%20x/simple",
            "https://nexus.example.:443/repo%20x/simple/itr/",
        ),
    ],
)
def test_project_url_rebuilds_the_authority(index_url: str, expected: str) -> None:
    """The URL requested and logged holds the host, port and path alone."""
    assert ci.project_url(index_url, "itr")[0] == expected


@pytest.mark.parametrize(
    "index_url",
    [
        "https://host.example::warning::injected/simple",
        "https://user:secret@host\uff1aexample/simple",
        "https://user:secret@host.example:99999/simple",
        "https://user:secret@[::1/simple",
        "https://host_name!x/simple",
    ],
)
def test_an_unusable_authority_is_refused_without_its_text(index_url: str) -> None:
    """No part of a refused authority, credentials included, reaches the error."""
    with pytest.raises(ci.QueryError) as caught:
        _ = ci.project_url(index_url, "itr")
    assert "secret" not in str(caught.value) and "::" not in str(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("path", ["simple", "html"])
@pytest.mark.parametrize(
    ("inputs", "code", "outputs"),
    [
        ({}, 0, {"package_match": "true", "version_match": "untested"}),
        (
            {"package_version": "1.1.10"},
            0,
            {"package_match": "true", "version_match": "true"},
        ),
        (
            {"package_version": "v1.1.10"},
            0,
            {"package_match": "true", "version_match": "true"},
        ),
        (
            {"package_version": "1.1.9.0"},
            0,
            {"package_match": "true", "version_match": "true"},
        ),
        (
            {"package_version": "13.69.666"},
            0,
            {"package_match": "true", "version_match": "false"},
        ),
        (
            {"package_version": "2.0.0RC1"},
            0,
            {"package_match": "true", "version_match": "false"},
        ),
        (
            {"package_version": "2.0.0RC1", "pre_release": "TRUE"},
            0,
            {"package_match": "true", "version_match": "true"},
        ),
        ({"package_version": "2.0.0.dev3", "exit_on_fail": "true"}, 1, {}),
        (
            {"package_name": "ITR-random-thing", "package_version": "1.0"},
            0,
            {"package_match": "false", "version_match": "untested"},
        ),
        ({"package_name": "ITR-random-thing", "exit_on_fail": "true"}, 1, {}),
    ],
)
def test_query(
    index: IndexServer,
    tmp_path: Path,
    path: str,
    inputs: dict[str, str],
    code: int,
    outputs: dict[str, str],
) -> None:
    """The outputs and status the integration workflow expects, on JSON and HTML pages."""
    run = run_script(
        tmp_path, {"package_name": "ITR", "index_url": base(index, path), **inputs}
    )
    assert (run.code, run.outputs) == (code, outputs), run.log


def test_a_project_page_with_no_distributions_is_not_a_package(
    index: IndexServer, tmp_path: Path
) -> None:
    """A page with nothing to install reports no package."""
    run = run_script(tmp_path, {"package_name": "empty", "index_url": base(index)})
    assert run.outputs == {"package_match": "false", "version_match": "untested"}


@pytest.mark.parametrize(
    "name",
    ["broken", "badjson", "plain", "tofile", "short", "chunked", "cuthtml", "charset"],
)
def test_an_unreadable_index_fails_without_exit_on_fail(
    index: IndexServer, tmp_path: Path, name: str
) -> None:
    """An index error fails the step rather than reporting the package missing."""
    run = run_script(tmp_path, {"package_name": name, "index_url": base(index)})
    assert (run.code, run.outputs) == (1, {}), run.log
    assert "NOT found" not in run.log


def test_a_url_the_parser_refuses_does_not_leak_credentials(tmp_path: Path) -> None:
    """urlsplit's own error quotes the authority; the step must not print it."""
    url = "https://user:secret@host\uff1aexample/simple"
    run = run_script(tmp_path, {"package_name": "itr", "index_url": url})
    assert (run.code, run.outputs) == (1, {}), run.log
    assert "secret" not in run.log and "Traceback" not in run.log


@pytest.mark.parametrize(
    "page",
    [
        b'<html><body><a href="itr-1.1.10.tar.gz"',
        b'<html><body><a href="itr-1.1.10.tar.gz">itr</a><a hr',
        b'<html><body><a href="http://[bad">x</a></body></html>',
        b"<html><body><!-- itr-1.1.10.tar.gz",
    ],
)
def test_a_truncated_or_malformed_html_page_is_an_error(page: bytes) -> None:
    """A page cut inside a tag, or with an unparsable link, is not an empty list."""
    with pytest.raises(ci.QueryError):
        _ = ci.filenames_from_page(page, "text/html", "utf-8")


def test_deeply_nested_json_is_an_error() -> None:
    """A page nested past the parser's recursion limit is malformed, not a crash."""
    with pytest.raises(ci.QueryError):
        _ = ci.filenames_from_page(b"[" * 200_000, JSON_TYPE, "utf-8")


@pytest.mark.parametrize(
    "page",
    [
        '{"meta": {"api-version": "1.1"}, "files": [null]}',
        '{"meta": {"api-version": "1.1"}, "files": [{}]}',
        '{"meta": {"api-version": "1.1"}, "files": [{"filename": 7}]}',
        '{"meta": {"api-version": "1.1"}, "files": [{"filename": ""}]}',
        '{"meta": {"api-version": "1.1"}, "files": ["itr-1.0.tar.gz"]}',
        '{"meta": {"api-version": "1.1"}, "files": [{"filename": "itr-1.0.tar.gz"}, null]}',
        '{"meta": {"api-version": "1.1"}}',
        '{"meta": {"api-version": "2.0"}, "files": []}',
        '{"files": []}',
        "[]",
    ],
)
def test_a_malformed_json_page_is_an_error_not_an_empty_list(page: str) -> None:
    """Only a well-formed page can say a project has no files."""
    with pytest.raises(ci.QueryError):
        _ = ci.filenames_from_page(page.encode(), JSON_TYPE, "utf-8")


def test_a_well_formed_empty_json_page_lists_nothing() -> None:
    """An empty ``files`` array is valid: the project has no distributions."""
    page = b'{"meta": {"api-version": "1.1"}, "files": []}'
    assert ci.filenames_from_page(page, JSON_TYPE, "utf-8") == []


def test_an_unreachable_index_fails(tmp_path: Path) -> None:
    """A refused connection fails the step."""
    run = run_script(
        tmp_path, {"package_name": "itr", "index_url": "http://127.0.0.1:9/simple"}
    )
    assert (run.code, run.outputs) == (1, {}), run.log


def test_a_same_scheme_redirect_is_followed(index: IndexServer, tmp_path: Path) -> None:
    """An http redirect within http is followed."""
    inputs = {
        "package_name": "itr",
        "index_url": base(index, "moved"),
        "package_version": "1.1.10",
    }
    run = run_script(tmp_path, inputs)
    assert run.outputs == {"package_match": "true", "version_match": "true"}, run.log


def test_credentials_authenticate_and_stay_out_of_the_log(
    index: IndexServer, tmp_path: Path
) -> None:
    """Credentials in the index URL reach the index as Basic auth and nowhere else."""
    host, port = index.server_address[:2]
    url = f"http://user:p%40ss@{host!s}:{port}/private"
    run = run_script(
        tmp_path, {"package_name": "itr", "index_url": url, "package_version": "1.1.10"}
    )
    assert run.outputs == {"package_match": "true", "version_match": "true"}, run.log
    assert "p%40ss" not in run.log and "user" not in run.log


@pytest.mark.parametrize(
    "inputs",
    [
        {"package_name": "::error::x"},
        {"package_name": "itr\n::stop-commands::x"},
        {"package_name": "itr", "package_version": "latest"},
    ],
)
def test_invalid_inputs_fail_before_any_request(
    index: IndexServer, tmp_path: Path, inputs: dict[str, str]
) -> None:
    """An unusable name or version fails without a query and without being echoed."""
    before = len(index.paths)
    run = run_script(tmp_path, {"index_url": base(index), **inputs})
    assert run.code == 1
    assert "::" not in run.log and "latest" not in run.log
    assert len(index.paths) == before


def test_workspace_modules_are_not_imported(index: IndexServer, tmp_path: Path) -> None:
    """A checkout holding stdlib-named modules, with Python settings pointing at it."""
    marker = tmp_path / "imported"
    workspace = tmp_path / "workspace"
    (workspace / "urllib").mkdir(parents=True)
    bait = f"open({str(marker)!r}, 'a').write(__file__)\n"
    for module in (
        "json.py",
        "base64.py",
        "sitecustomize.py",
        "usercustomize.py",
        "urllib/__init__.py",
    ):
        _ = (workspace / module).write_text(bait)
    env = {
        "PATH": os.environ["PATH"],
        "PYTHONPATH": str(workspace),
        "PYTHONSTARTUP": str(workspace / "json.py"),
        "PYTHONUSERBASE": str(workspace),
        "INPUT_PACKAGE_NAME": "itr",
        "INPUT_INDEX_URL": base(index),
    }
    proc = subprocess.run(
        [sys.executable, "-I", str(SCRIPT)],
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not marker.exists()


def step(name: str) -> dict[str, object]:
    """One step of action.yaml, by name."""
    action = cast(
        "dict[str, dict[str, list[dict[str, object]]]]",
        yaml.safe_load((REPO_ROOT / "action.yaml").read_text()),
    )
    for entry in action["runs"]["steps"]:
        if entry.get("name") == name:
            return entry
    raise AssertionError(f"step not found in action.yaml: {name}")


def mapping(entry: dict[str, object], key: str) -> dict[str, str]:
    """A step's ``with`` or ``env`` block."""
    return cast("dict[str, str]", entry[key])


def text(entry: dict[str, object], key: str) -> str:
    """A step's string field."""
    return cast("str", entry[key])


INSTALL_DIR = "${{ runner.temp }}/pypi-version-check-action/python"


def test_action_runs_the_script_isolated_from_the_workspace() -> None:
    """uv and the script run from the action's directory, the script under -I."""
    setup = step("Install uv")
    assert mapping(setup, "with")["working-directory"] == "${{ github.action_path }}"
    # Without it setup-uv puts the caller's HOME or XDG_* bin directory on PATH
    assert mapping(setup, "with")["tool-bin-dir"].startswith("${{ runner.temp }}/")
    # UV_NO_MODIFY_PATH is deliberately absent: setup-uv's own bare "uv"
    # call would then resolve on the caller's PATH
    assert mapping(setup, "env") == {
        "UV_NO_CONFIG": "1",
        "UV_NO_PROJECT": "1",
        "UV_PYTHON_PREFERENCE": "lookup-disabled",
        "UV_PYTHON_ARCH": "",
        "UV_PYTHON_INSTALL_DIR": INSTALL_DIR,
    }
    install = step("Install Python")
    assert mapping(install, "env")["UV"] == "${{ steps.setup-uv.outputs.uv-path }}"
    assert mapping(install, "env")["INSTALL_DIR"] == INSTALL_DIR
    check = step("Check Python package index")
    assert text(check, "working-directory") == "${{ github.action_path }}"
    assert mapping(check, "env")["PYTHON"] == "${{ steps.python.outputs.path }}"
    assert '"$PYTHON" -I "$GITHUB_ACTION_PATH/scripts/check_index.py"' in text(
        check, "run"
    )
    assert "unset PYTHONEXECUTABLE __PYVENV_LAUNCHER__" in text(check, "run")


def plant_interpreters(tmp_path: Path) -> tuple[dict[str, str], Path]:
    """Bait interpreters, and every caller setting known to point uv at one."""
    marker = tmp_path / "bait-ran"
    bait = tmp_path / "workspace" / ".venv"
    (bait / "bin").mkdir(parents=True)
    (bait / "conda-meta").mkdir()
    _ = (bait / "pyvenv.cfg").write_text("home = /usr/bin\n")
    for name in ("python", "python3", "python3.13"):
        tool = bait / "bin" / name
        _ = tool.write_text(f"#!/bin/sh\necho {name} >> '{marker}'\nexit 1\n")
        tool.chmod(0o755)
    interpreter = str(bait / "bin" / "python3")
    planted = {
        "PATH": f"{bait}/bin:{os.environ['PATH']}",
        "VIRTUAL_ENV": str(bait),
        "CONDA_PREFIX": str(bait),
        "CONDA_DEFAULT_ENV": "base",
        "UV_INTERNAL__PARENT_INTERPRETER": interpreter,
        "UV_INTERNAL__TEST_PYTHON_MANAGED": interpreter,
        "UV_PYTHON_SEARCH_PATH": str(bait / "bin"),
        "UV_PYTHON": interpreter,
        "UV_SYSTEM_PYTHON": "1",
        "UV_PROJECT_ENVIRONMENT": str(bait),
        "UV_PYTHON_INSTALL_MIRROR": "https://evil.example",
        "UV_PYTHON_DOWNLOADS_JSON_URL": "https://evil.example/list.json",
        "UV_CACHE_DIR": str(bait),
        "PYTHONEXECUTABLE": interpreter,
        "__PYVENV_LAUNCHER__": interpreter,
    }
    return planted, marker


@pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv on PATH")
def test_setup_uv_lookup_refuses_before_running_anything(tmp_path: Path) -> None:
    """setup-uv's "uv python find", under its step env, runs no interpreter."""
    uv = cast("str", shutil.which("uv"))
    planted, marker = plant_interpreters(tmp_path)
    action_dir = tmp_path / "action"
    action_dir.mkdir()
    control = subprocess.run(
        [uv, "python", "find", "--directory", str(action_dir)],
        env={**planted, "UV_NO_CACHE": "1"},
        capture_output=True,
        check=False,
    )
    assert marker.exists(), control  # the bait is live for a plain lookup
    marker.unlink()
    step_env = {**planted, "UV_NO_CACHE": "1", **mapping(step("Install uv"), "env")}
    refused = subprocess.run(
        [uv, "python", "find", "--directory", str(action_dir)],
        env=step_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert refused.returncode != 0 and "lookup-disabled" in refused.stderr
    assert not marker.exists()


def run_install_step(
    tmp_path: Path, answer: str, extra: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run the install step's script with a stub uv that records its environment."""
    stub = tmp_path / "uv"
    log = tmp_path / "env"
    _ = stub.write_text(f"#!/bin/sh\nenv | sort >> '{log}'\necho '{answer}'\n")
    stub.chmod(0o755)
    (tmp_path / "out").touch()
    env = {
        "PATH": os.environ["PATH"],
        "UV": str(stub),
        "GITHUB_OUTPUT": str(tmp_path / "out"),
        "PYTHON_VERSION": "3.13",
        "INSTALL_DIR": "/runner/temp/python",
        "CACHE_DIR": "/runner/temp/cache",
        **extra,
    }
    return subprocess.run(
        [*BASH, text(step("Install Python"), "run")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_install_step_gives_uv_nothing_from_the_caller(tmp_path: Path) -> None:
    """uv sees the step's own settings and the proxy ones, and no other variable."""
    planted, _ = plant_interpreters(tmp_path)
    proxies = {"HTTPS_PROXY": "http://proxy:3128", "SSL_CERT_FILE": "/etc/ca.pem"}
    proc = run_install_step(
        tmp_path,
        "/runner/temp/python/cpython-3.13/bin/python3.13",
        {**planted, **proxies},
    )
    assert proc.returncode == 0, proc.stderr
    # Each of the two uv calls logs its whole environment; the shell adds these
    seen = set((tmp_path / "env").read_text().splitlines())
    assert {line for line in seen if not line.startswith(("PWD=", "SHLVL=", "_="))} == {
        "UV_NO_CONFIG=1",
        "UV_NO_PROJECT=1",
        "UV_PYTHON_INSTALL_DIR=/runner/temp/python",
        "UV_CACHE_DIR=/runner/temp/cache",
        "HTTPS_PROXY=http://proxy:3128",
        "SSL_CERT_FILE=/etc/ca.pem",
    }
    assert (
        tmp_path / "out"
    ).read_text() == "path=/runner/temp/python/cpython-3.13/bin/python3.13\n"


@pytest.mark.parametrize(
    "answer",
    ["/workspace/.venv/bin/python3", "/runner/temp/python-evil/bin/python3", ""],
)
def test_install_step_refuses_python_outside_the_install_directory(
    tmp_path: Path, answer: str
) -> None:
    """A path uv reports outside its install directory stops the step."""
    proc = run_install_step(tmp_path, answer, {})
    assert proc.returncode == 1
    assert (tmp_path / "out").read_text() == ""


def test_action_never_runs_pip_or_a_path_python() -> None:
    """No step names pip or a Python looked up on PATH."""
    action = (REPO_ROOT / "action.yaml").read_text()
    assert "pip" not in action and "python3" not in action
