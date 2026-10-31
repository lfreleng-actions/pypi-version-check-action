# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
# Annotations raise TypeError on old Pythons this must run on; see below.
# pyright: reportTypeCommentUsage=false

"""Print shell settings that keep the workspace off pip's import path.

The working directory is the caller's workspace. Two settings a
caller passes in can still put it on sys.path:

- a PYTHONPATH entry that is empty or relative (both resolved against
  the working directory), that resolves inside the workspace, or that
  is a parent of it, so the checkout imports as a package named after
  its directory; a module there would load in place of pip's own, and
  a sitecustomize.py there would run at start-up;
- a user site directory inside the workspace (PYTHONUSERBASE, or HOME
  when that is unset); Python runs the .pth files it finds there at
  every start-up.

The output is shell code for 'eval': PYTHONPATH keeps only absolute
entries outside the workspace and not above it, such as a 'pip install
--target' directory, or is unset when none remain. A user site inside
the workspace is replaced: PYTHONUSERBASE moves to FALLBACK_USER_BASE,
an absolute directory outside the workspace that the steps share, so
pip can still fall back to a user install there; without one,
PYTHONNOUSERSITE=1 turns the user site off. A user site elsewhere is
left alone.

Run it with -I -S, so that this script ignores PYTHONPATH and the
working directory and runs no .pth file. It runs on any Python 3 a
runner may have, so its types are given as comments, which no version
of Python evaluates.
"""

import os
import shlex
import site


def _inside(path, roots):
    # type: (str, list[str]) -> bool
    """True when ``path`` resolves to one of ``roots`` or below it."""
    real = os.path.realpath(path)
    return any(
        real == root or real.startswith(root.rstrip(os.sep) + os.sep) for root in roots
    )


def _above(path, roots):
    # type: (str, list[str]) -> bool
    """True when ``path`` resolves to a directory holding one of ``roots``."""
    real = os.path.realpath(path).rstrip(os.sep) + os.sep
    return any(root.startswith(real) for root in roots)


def trusted_pythonpath(pythonpath, roots):
    # type: (str, list[str]) -> str
    """Keep the absolute entries of ``pythonpath`` neither in nor above a root."""
    kept = [
        entry
        for entry in pythonpath.split(os.pathsep)
        if os.path.isabs(entry)
        and not _inside(entry, roots)
        and not _above(entry, roots)
    ]
    return os.pathsep.join(kept)


def shell_settings(pythonpath, user_site, roots, fallback_user_base=""):
    # type: (str, str, list[str], str) -> str
    """Shell code that applies the settings described in the module docstring."""
    real_roots = [os.path.realpath(root) for root in roots if root]
    kept = trusted_pythonpath(pythonpath, real_roots)
    lines = ["export PYTHONPATH=" + shlex.quote(kept) if kept else "unset PYTHONPATH"]
    if user_site and _inside(user_site, real_roots):
        usable = (
            os.path.isabs(fallback_user_base)
            and not _inside(fallback_user_base, real_roots)
            and not _above(fallback_user_base, real_roots)
        )
        if usable:
            lines.append("export PYTHONUSERBASE=" + shlex.quote(fallback_user_base))
        else:
            lines.append("export PYTHONNOUSERSITE=1")
    return "\n".join(lines)


if __name__ == "__main__":
    workspace_roots = [os.getcwd(), os.environ.get("GITHUB_WORKSPACE", "")]
    print(
        shell_settings(
            os.environ.get("PYTHONPATH", ""),
            site.getusersitepackages(),
            workspace_roots,
            os.environ.get("FALLBACK_USER_BASE", ""),
        )
    )
