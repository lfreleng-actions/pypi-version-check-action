# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Check a Python package index for a project and, optionally, a version.

Reads the project's page from a PEP 503 or PEP 691 simple index,
collects the versions of its wheels and source distributions, and
writes ``package_match`` and ``version_match`` to ``GITHUB_OUTPUT``.

Standard library only: the action runs this with ``python -I`` under
an interpreter uv installs outside the workspace, so nothing in the
caller's checkout or environment can supply a module it imports.

Exit status: 0 when the query ran, 1 on an unusable input, an index
that cannot be read, or a failed match with ``EXIT_ON_FAIL`` set.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email.message import Message
from html.parser import HTMLParser
from http.client import HTTPException, HTTPMessage, HTTPResponse
from typing import IO, cast, override

TIMEOUT_SECONDS = 60
MAX_RESPONSE_BYTES = 128 * 1024 * 1024
JSON_TYPE = "application/vnd.pypi.simple.v1+json"
HTML_TYPES = ("text/html", "application/vnd.pypi.simple.v1+html")
ACCEPT = f"{JSON_TYPE}, application/vnd.pypi.simple.v1+html;q=0.2, text/html;q=0.1"

# PEP 508 project names.
NAME_RE = re.compile(r"^([A-Z0-9]|[A-Z0-9][A-Z0-9._-]*[A-Z0-9])$", re.IGNORECASE)

# An index host: a DNS name, an IPv4 address or an IPv6 literal.
HOST_RE = re.compile(r"^(?:[a-z0-9-]+(?:\.[a-z0-9-]+)*\.?|[0-9a-f:.]*:[0-9a-f:.]*)$")

# PEP 440, Appendix B: the canonical public version regex.
VERSION_RE = re.compile(
    r"""
    ^\s*v?
    (?:(?P<epoch>[0-9]+)!)?
    (?P<release>[0-9]+(?:\.[0-9]+)*)
    (?P<pre>[-_.]?(?P<pre_l>a|b|c|rc|alpha|beta|pre|preview)[-_.]?(?P<pre_n>[0-9]+)?)?
    (?P<post>(?:-(?P<post_n1>[0-9]+))|(?:[-_.]?(?P<post_l>post|rev|r)[-_.]?(?P<post_n2>[0-9]+)?))?
    (?P<dev>[-_.]?(?P<dev_l>dev)[-_.]?(?P<dev_n>[0-9]+)?)?
    (?:\+(?P<local>[a-z0-9]+(?:[-_.][a-z0-9]+)*))?
    \s*$
    """,
    re.VERBOSE | re.IGNORECASE,
)

PRE_SPELLINGS = {"alpha": "a", "beta": "b", "c": "rc", "pre": "rc", "preview": "rc"}

# Archive suffixes pip accepts for a source distribution.
SDIST_SUFFIXES = (
    ".tar.gz",
    ".tgz",
    ".tar.bz2",
    ".tbz",
    ".tar.xz",
    ".txz",
    ".tar.lz",
    ".tlz",
    ".tar.lzma",
    ".tar",
    ".zip",
)


class QueryError(Exception):
    """The index could not be read; never reported as a missing package."""


@dataclass(frozen=True)
class Version:
    """A parsed PEP 440 version: its normalised text and comparison key."""

    text: str
    key: str
    is_prerelease: bool


def normalise_name(name: str) -> str:
    """PEP 503 normalised project name."""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_version(raw: str) -> Version | None:
    """Parse a PEP 440 version, or return None when it is not one.

    ``text`` is the normalised form pip prints. ``key`` also drops
    trailing zero release segments, so ``1.0`` and ``1.0.0`` compare
    equal, as PEP 440 and the index's upload check treat them.
    """
    found = VERSION_RE.match(raw)
    if not found:
        return None
    release = [int(part) for part in found["release"].split(".")]
    suffix = ""
    if found["pre"]:
        label = found["pre_l"].lower()
        suffix += f"{PRE_SPELLINGS.get(label, label)}{int(found['pre_n'] or 0)}"
    if found["post"]:
        suffix += f".post{int(found['post_n1'] or found['post_n2'] or 0)}"
    if found["dev"]:
        suffix += f".dev{int(found['dev_n'] or 0)}"
    if found["local"]:
        segments = re.split(r"[-_.]", found["local"].lower())
        suffix += "+" + ".".join(str(int(s)) if s.isdigit() else s for s in segments)
    epoch = int(found["epoch"] or 0)
    prefix = f"{epoch}!" if epoch else ""
    significant = len(release)
    while significant > 1 and release[significant - 1] == 0:
        significant -= 1
    return Version(
        text=prefix + ".".join(map(str, release)) + suffix,
        key=prefix + ".".join(map(str, release[:significant])) + suffix,
        is_prerelease=bool(found["pre"] or found["dev"]),
    )


def version_from_filename(filename: str, project: str) -> str | None:
    """The version a wheel or sdist filename carries, or None for other files.

    The name part must normalise to the project's name, as pip
    requires; an sdist name may itself hold hyphens, so each split
    point is tried.
    """
    if filename.endswith(".whl"):
        parts = filename[: -len(".whl")].split("-")
        if len(parts) in (5, 6) and normalise_name(parts[0]) == project:
            return parts[1]
        return None
    lowered = filename.lower()
    suffix = next((s for s in SDIST_SUFFIXES if lowered.endswith(s)), None)
    if suffix is None:
        return None
    stem = filename[: -len(suffix)]
    for index, char in enumerate(stem):
        if char == "-" and normalise_name(stem[:index]) == project:
            return stem[index + 1 :] or None
    return None


class _AnchorParser(HTMLParser):
    """Collects the filename each anchor's href ends with."""

    def __init__(self) -> None:
        super().__init__()
        self.filenames: list[str] = []

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        href = dict(attrs).get("href")
        if href:
            path = urllib.parse.urlsplit(href).path
            self.filenames.append(urllib.parse.unquote(path.rsplit("/", 1)[-1]))


def _json_filenames(body: bytes) -> list[str]:
    """The filenames a PEP 691 JSON page lists."""
    try:
        page = cast("object", json.loads(body))
    except (ValueError, RecursionError) as exc:
        raise QueryError("malformed JSON project page") from exc
    if not isinstance(page, dict):
        raise QueryError("malformed JSON project page")
    fields = cast("dict[str, object]", page)
    meta, files = fields.get("meta"), fields.get("files")
    if not isinstance(meta, dict) or not isinstance(files, list):
        raise QueryError("malformed JSON project page")
    api = cast("dict[str, object]", meta).get("api-version")
    if not isinstance(api, str) or not api.startswith("1."):
        raise QueryError("unsupported simple API version on the index")
    names: list[str] = []
    for item in cast("list[object]", files):
        filename = (
            cast("dict[str, object]", item).get("filename")
            if isinstance(item, dict)
            else None
        )
        if not isinstance(filename, str) or not filename:
            raise QueryError("malformed file entry on the JSON project page")
        names.append(filename)
    return names


def _html_filenames(body: bytes, charset: str) -> list[str]:
    """The filenames a PEP 503 HTML page lists.

    Markup the parser still holds after the whole body is fed is a tag
    the page never finished, so the page was cut short.
    """
    try:
        text = body.decode(charset, errors="replace")
    except LookupError as exc:
        raise QueryError("the index named an unknown character set") from exc
    parser = _AnchorParser()
    try:
        parser.feed(text)
    except ValueError as exc:
        raise QueryError("malformed HTML project page") from exc
    if parser.rawdata.strip():
        raise QueryError("truncated HTML project page")
    parser.close()
    return parser.filenames


def filenames_from_page(body: bytes, content_type: str, charset: str) -> list[str]:
    """The distribution filenames a project page lists."""
    media = content_type.split(";", 1)[0].strip().lower()
    if media == JSON_TYPE:
        return _json_filenames(body)
    if media in HTML_TYPES:
        return _html_filenames(body, charset)
    raise QueryError("the index answered with an unexpected content type")


class _SameSchemeRedirects(urllib.request.HTTPRedirectHandler):
    """Follows redirects within http(s), never from https down to http."""

    @override
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        old = urllib.parse.urlsplit(req.full_url).scheme
        new = urllib.parse.urlsplit(newurl).scheme
        if new not in ("http", "https") or (old == "https" and new != "https"):
            raise QueryError("the index redirected to a disallowed URL scheme")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def project_url(index_url: str, project: str) -> tuple[str, str | None]:
    """The project page URL, without credentials, and any Basic credentials.

    The authority is rebuilt from the parsed host and port, so the URL
    requested and logged holds nothing else from the input. A parse
    error's text can quote the credentials, so none is passed on.
    """
    try:
        parts = urllib.parse.urlsplit(index_url.strip())
        host, port = parts.hostname or "", parts.port
        username, password = parts.username, parts.password
    except ValueError:
        raise QueryError("the index URL is not a valid URL") from None
    if parts.scheme not in ("http", "https") or not HOST_RE.match(host):
        raise QueryError("the index URL must be an http or https URL with a host")
    credentials = None
    if username is not None:
        user = urllib.parse.unquote(username)
        secret = urllib.parse.unquote(password or "")
        credentials = base64.b64encode(f"{user}:{secret}".encode()).decode()
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc += f":{port}"
    path = urllib.parse.quote(parts.path.rstrip("/"), safe="/%~") + f"/{project}/"
    return urllib.parse.urlunsplit((parts.scheme, netloc, path, "", "")), credentials


def fetch_filenames(index_url: str, project: str) -> list[str] | None:
    """The filenames on the project's page, or None when the index has no such project."""
    url, credentials = project_url(index_url, project)
    request = urllib.request.Request(
        url, headers={"Accept": ACCEPT, "User-Agent": "pypi-version-check-action"}
    )
    if credentials:
        # Unredirected, so a redirect to another host never receives it.
        request.add_unredirected_header("Authorization", f"Basic {credentials}")
    opener = urllib.request.build_opener(_SameSchemeRedirects())
    try:
        response = cast("HTTPResponse", opener.open(request, timeout=TIMEOUT_SECONDS))
        with response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
            unread = response.length
            headers: Message = response.headers
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise QueryError(f"the index answered HTTP {exc.code}") from exc
    except (OSError, HTTPException) as exc:
        reason = cast("object", getattr(exc, "reason", exc))
        raise QueryError(
            f"the index could not be read ({type(reason).__name__})"
        ) from exc
    except ValueError:
        raise QueryError("the index answered with an unusable URL") from None
    if len(body) > MAX_RESPONSE_BYTES:
        raise QueryError("the project page exceeds the size limit")
    if unread:
        raise QueryError("the index closed the connection mid-response")
    content_type = headers.get("Content-Type") or ""
    charset = headers.get_content_charset() or "utf-8"
    return filenames_from_page(body, content_type, charset)


def available_versions(
    filenames: list[str], project: str, pre_release: bool
) -> list[Version]:
    """The distinct versions the filenames carry, pre-releases only when asked."""
    seen: dict[str, Version] = {}
    for filename in filenames:
        raw = version_from_filename(filename, project)
        version = parse_version(raw) if raw else None
        if (
            version
            and (pre_release or not version.is_prerelease)
            and version.text not in seen
        ):
            seen[version.text] = version
    return list(seen.values())


def flag(name: str) -> bool:
    """An action input read as a boolean; anything but 'true' is false."""
    return os.environ.get(name, "").strip().lower() == "true"


def write_outputs(package_match: str, version_match: str) -> None:
    """Append the step outputs to GITHUB_OUTPUT, when the runner provides it."""
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            _ = handle.write(
                f"package_match={package_match}\nversion_match={version_match}\n"
            )


def main() -> int:
    """Run the query from the action's inputs; return the exit status."""
    index_url = os.environ.get("INPUT_INDEX_URL", "")
    name = os.environ.get("INPUT_PACKAGE_NAME", "").strip()
    requested_raw = os.environ.get("INPUT_PACKAGE_VERSION", "").strip()
    pre_release = flag("INPUT_PRE_RELEASE")
    exit_on_fail = flag("INPUT_EXIT_ON_FAIL")

    if not NAME_RE.match(name):
        print("Error: package_name is not a valid Python project name ❌")
        return 1
    requested = None
    if requested_raw:
        if requested_raw[0] in "vV":
            print("Stripping non-numeric leading character from version string ⚠️")
        requested = parse_version(requested_raw)
        if requested is None:
            print("Error: package_version is not a valid PEP 440 version ❌")
            return 1

    project = normalise_name(name)
    try:
        url, _ = project_url(index_url, project)
        print(f"Searching index:   {url}")
        print(
            f"For package:       {name} {requested.text if requested else ''}".rstrip()
        )
        if pre_release:
            print("Including pre-release and development versions 💬")
        filenames = fetch_filenames(index_url, project)
    except QueryError as exc:
        print(f"Error: {exc} ❌")
        return 1

    versions = available_versions(filenames or [], project, pre_release=True)
    query_error = False
    version_match = "untested"
    if filenames is None or not versions:
        package_match = "false"
        query_error = True
        print(f"Package {name} NOT found in index ❌")
    else:
        package_match = "true"
        print(f"Package {name} found in index ✅")
        if requested:
            visible = available_versions(filenames, project, pre_release)
            if any(v.key == requested.key for v in visible):
                version_match = "true"
                print(f"Version {requested.text} was found in index ✅")
            else:
                version_match = "false"
                query_error = True
                print(f"Version {requested.text} was NOT found in index ❌")
                if requested.is_prerelease and not pre_release:
                    print("Set pre_release to true to match pre-release versions")

    if exit_on_fail and query_error:
        print("Error: a query failed to return results ❌")
        return 1
    write_outputs(package_match, version_match)
    return 0


if __name__ == "__main__":
    sys.exit(main())
