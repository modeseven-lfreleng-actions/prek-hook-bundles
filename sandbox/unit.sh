#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

# One unit build: install the hook environments of the configuration
# on standard input. This is where a hook repository's own install
# code runs, so it runs in a container of its own, started from the
# base image and thrown away afterwards. The builder keeps nothing of
# it but the new directories under hooks/, and checks those before
# they reach a bundle.
#
# Environment: PREK_HOME.

set -euo pipefail

mkdir unit
cd unit
git init --quiet .
cat > .pre-commit-config.yaml
exec prek prepare-hooks
