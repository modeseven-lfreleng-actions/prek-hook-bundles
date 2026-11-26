# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Run the command line by path: python3 -I cli.py <command>.

The action runs the installer this way. Under -I Python leaves the
working directory, which is the checkout being linted, and PYTHONPATH
off the import path, so nothing in a pull request can stand in for a
module this code imports. That also hides the hookbundles package, so
this file puts its own directory, and only that, back.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hookbundles.__main__ import main  # noqa: E402 - after the path is set

sys.exit(main())
