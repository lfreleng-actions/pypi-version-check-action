<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2024 The Linux Foundation
-->

# 📦 PyPI Package Check

Checks the Python package index for the presence of a package (and
optionally a specific release/version).

## pypi-version-check-action

## Usage Example

Pass the index query URL, along with Python package name and optionally the
package release/version to check.

```yaml
  - name: "Checking package index for build/release"
    id: check-package-index
    uses: lfreleng-actions/pypi-version-check-action@main
    with:
        index_url: "https://pypi.org/simple"
        package_name: "ITR"
        package_version: "1.1.10"
```

Query URL examples:

- PyPI <https://pypi.org/simple>
- TestPyPI <https://test.pypi.org/simple>

## Inputs

<!-- markdownlint-disable MD013 -->

| Variable Name   | Required | Default                   | Description                                               |
| --------------- | -------- | ------------------------- | --------------------------------------------------------- |
| PACKAGE_NAME    | True     |                           | Name of Python package to check                           |
| PACKAGE_VERSION | False    |                           | Optional release/version to check                         |
| INDEX_URL       | False    | <https://pypi.org/simple> | The Python package index server URL to query              |
| PRE_RELEASE     | False    | False                     | Lets VERSION_MATCH match pre-release/development versions |
| EXIT_ON_FAIL    | False    | False                     | Set true to exit with error on failure                    |

<!-- markdownlint-enable MD013 -->

Note: Leading "v" characters are automatically stripped, as the Python package
index uses purely numerical versioning. Versions compare as PEP 440 defines:
`1.0.0RC1` matches `1.0.0rc1`, and `1.0` matches `1.0.0`. A version that is
not valid PEP 440, or a name that is not a valid project name, fails the step.

The index URL may carry credentials (`https://user:token@host/simple`); the
action sends them as HTTP Basic authentication to that host alone and never
prints them.

## Outputs

<!-- markdownlint-disable MD013 -->

| Variable Name | Mandatory | Description                                    |
| ------------- | --------- | ---------------------------------------------- |
| PACKAGE_MATCH | True      | Always reports the presence of the project     |
| VERSION_MATCH | False     | Reports a match when specific version provided |

<!-- markdownlint-enable MD013 -->

## Implementation Details

The action reads the project's page from the index's simple API, asking for
the PEP 691 JSON form and accepting the PEP 503 HTML form, and collects the
versions of the wheels and source distributions it lists. A 404 means the
project is absent; any other failure to read the index fails the step,
whatever `EXIT_ON_FAIL` says, rather than reporting the package missing.

`PRE_RELEASE` governs `VERSION_MATCH` alone: a pre-release or development
version matches when `PRE_RELEASE` is true, and never otherwise.
`PACKAGE_MATCH` reports whether the project lists any distribution at all,
so a project with nothing but pre-releases reports true whatever
`PRE_RELEASE` says. Yanked releases count, since the index still holds
them. The check is for presence in the index, not installability, so it
ignores the files' Python and platform requirements.

The query is a standard-library Python script, `scripts/check_index.py`. The
action never runs `pip` or a Python from the caller's `PATH`: it installs
uv with `setup-uv`, pinned by `requirements/uv.txt`, has uv install its own
hash-checked Python under the runner's temporary directory, and runs the
script with `python -I` from the action's directory. Nothing in the caller's
checkout, and no `PYTHON*` or `UV_*` setting the caller exports, can supply
code it runs. The interpreter download comes from `releases.astral.sh`,
which an egress allow-list must permit.

The action trusts the caller's `PATH`, as every composite action must: the
runner finds `bash` there for each step, and `setup-uv` runs `ldd` and `tar`
from it. A job that puts a directory from its checkout on `PATH` runs that
checkout's code whatever any action does.

## Development

The tests under `tests/` run with [uv](https://docs.astral.sh/uv/), which
installs the versions pinned in `uv.lock` into a local `.venv`:

```bash
uv sync --frozen
uv run --frozen pytest
```
