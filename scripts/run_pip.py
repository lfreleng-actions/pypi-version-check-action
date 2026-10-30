# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
# Annotations raise TypeError on old Pythons this must run on.
# pyright: reportTypeCommentUsage=false

"""Run pip as 'python3 -m pip' would, without the workspace on its import path.

'python3 -m pip' puts the working directory, the caller's workspace,
first on sys.path, so a pip/ package there would be imported in place
of the installed one. Run as a script, Python puts this file's
directory first instead, and the working directory stays where pip
expects it: relative paths in PIP_* settings and pip.conf still
resolve against the workspace, and the user site is still searched.

The steps start this script without PYTHONPATH: its entries would come
before the standard library, and Python imports sitecustomize from them
at start-up. The entries the steps keep (see trusted_pythonpath.py)
arrive in ACTION_PYTHONPATH instead, and go after the standard library
and before the site directories, so a pip installed with --target is
still found ahead of site-packages, as with PYTHONPATH.

pip itself must then resolve outside the workspace and not above it,
and so must every module imported while it runs: the entry point
(pip/__main__.py), each of pip's own modules and anything else, as a
file in a directory outside the workspace can be a link into it, and
an import hook from a .pth file can map a module there without adding
a path. A finder placed first on sys.meta_path finds each module as
the import system would, through the finders after it, checks where
it would load from, and hands that spec back, so the module loads
from the place checked; one that resolves to the workspace stops the
run, as if pip were missing.
"""

import importlib.abc
import importlib.machinery
import importlib.util
import os
import runpy
import site
import sys
import typing

# Run as a script, this file's directory is first on sys.path.
import trusted_pythonpath  # pyright: ignore[reportImplicitRelativeImport]


def _site_dirs():
    # type: () -> list[str]
    """The site directories on sys.path, as 'site' added them."""
    # Old virtualenv releases replace site.py with one lacking this
    found = site.getsitepackages() if hasattr(site, "getsitepackages") else []
    if site.ENABLE_USER_SITE:
        found.append(site.getusersitepackages())
    return [os.path.normcase(os.path.abspath(path)) for path in found]


def add_action_pythonpath(entries):
    # type: (list[str]) -> None
    """Put ``entries`` on sys.path ahead of the first site directory."""
    site_dirs = _site_dirs()
    index = len(sys.path)
    for position, path in enumerate(sys.path):
        if os.path.normcase(os.path.abspath(path)) in site_dirs:
            index = position
            break
    sys.path[index:index] = [entry for entry in entries if entry]


def spec_locations(spec):
    # type: (importlib.machinery.ModuleSpec) -> list[str]
    """Where a module found as ``spec`` would load from."""
    found = list(spec.submodule_search_locations or [])
    if spec.has_location and spec.origin:
        found.append(spec.origin)
    return found


# Before 3.12, a finder with find_module and no find_spec is still
# asked, and the loader it returns is wrapped as a spec.
LEGACY_FINDERS = sys.version_info < (3, 12)


def find_as_import_system(finder, name, path, target):
    # type: (object, str, object, object) -> importlib.machinery.ModuleSpec | None
    """The spec ``finder`` gives ``name``, found as importlib._bootstrap does."""
    find_spec = typing.cast("object", getattr(finder, "find_spec", None))
    find_module = typing.cast("object", getattr(finder, "find_module", None))
    if callable(find_spec):
        spec = find_spec(name, path, target)
    elif LEGACY_FINDERS and callable(find_module):
        loader = find_module(name, path)
        if loader is None:
            return None
        spec = importlib.util.spec_from_loader(
            name, typing.cast("importlib.abc.Loader", loader)
        )
    else:
        return None
    if isinstance(spec, importlib.machinery.ModuleSpec):
        return spec
    return None


class WorkspaceGuard:
    """A meta path finder that refuses modules resolving to the workspace.

    It finds each module as the import system would, through the
    finders after it, checks every location the spec names, and hands
    the spec back, so the module loads from the place checked and no
    finder is asked twice. Stopping happens here, before the module
    loads, and ends the process directly: pip catches BaseException
    around its commands, so a SystemExit raised during one of its
    imports would otherwise turn into a logged error.
    """

    def __init__(self, roots):
        # type: (list[str]) -> None
        """Refuse modules resolving inside or above ``roots`` (already resolved)."""
        self.roots = roots  # type: list[str]

    def find_spec(self, name, path=None, target=None):
        # type: (str, object, object) -> importlib.machinery.ModuleSpec | None
        """Find ``name`` through the other finders, checking where it loads from.

        The arguments are forwarded as the import system gave them. A
        finder may remove itself from sys.meta_path while it looks, so
        the list is walked as it stood.
        """
        for finder in list(sys.meta_path):
            if finder is self:
                continue
            spec = find_as_import_system(finder, name, path, target)
            if spec is None:
                continue
            for location in spec_locations(spec):
                if trusted_pythonpath.reaches_workspace(location, self.roots):
                    _ = sys.stderr.write(
                        name + " resolves to the workspace: " + location + "\n"
                    )
                    _ = sys.stderr.flush()
                    os._exit(1)
            return spec
        return None


if __name__ == "__main__":
    add_action_pythonpath(os.environ.get("ACTION_PYTHONPATH", "").split(os.pathsep))
    workspace_roots = [
        os.path.realpath(root)
        for root in (os.getcwd(), os.environ.get("GITHUB_WORKSPACE", ""))
        if root
    ]
    sys.meta_path.insert(0, WorkspaceGuard(workspace_roots))
    # pip ends the process through SystemExit; the namespace is unused.
    _ = runpy.run_module("pip", run_name="__main__", alter_sys=True)
