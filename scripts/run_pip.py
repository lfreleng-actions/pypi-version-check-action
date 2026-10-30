# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Run pip as 'python3 -m pip' would, without the working directory on sys.path.

'python3 -m pip' puts the working directory, the caller's workspace,
first on sys.path, so a pip/ package there would be imported in place
of the installed one. Run as a script, Python puts this file's
directory first instead, and the working directory stays where pip
expects it: relative paths in PIP_* settings and pip.conf still
resolve against the workspace, and the user site is still searched.
"""

import runpy

if __name__ == "__main__":
    # pip ends the process through SystemExit; the namespace is unused.
    _ = runpy.run_module("pip", run_name="__main__", alter_sys=True)
