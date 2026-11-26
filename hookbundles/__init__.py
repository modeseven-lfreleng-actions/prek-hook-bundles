# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Build, check and install bundles of prebuilt prek hook environments.

See docs/DESIGN.md for what a bundle is and why it is built this way.
"""

# The name under which the installer leaves a copy of the bundle's
# index at the root of the prek store, recording what it installed.
MANIFEST_NAME = "hook-bundle.json"

# The index layout this code writes and reads.
SCHEMA = 1
