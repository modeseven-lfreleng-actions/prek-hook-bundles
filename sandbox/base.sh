#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

# The trusted base stage, run once in the sandbox image.
#
# It seeds the prek store with the toolchains bundle.toml names and
# clones every hook repository the plan lists. It installs no hook:
# the seed configuration holds local hooks with no dependencies, and
# 'prek list' reads a repository's manifest without building anything.
# So nothing a hook repository ships runs here, and what this stage
# leaves in tools/ and repos/ is what every unit build starts from.
#
# Inputs, copied in before the container starts:
#   ~/in/seed.json      the seed configuration
#   ~/in/units/*.json   one configuration per planned unit
# Environment: PREK_HOME, PYTHON_VERSION.
# Output: one 'CLONE <unit> <ok|failed>' line per unit, then
# 'PREK <version>' and 'ARCH <machine>'.

set -euo pipefail

input="${HOME}/in"

# prek prefers a Python it manages over one it finds, so installing
# the pinned release into its store is what selects it.
UV_PYTHON_INSTALL_DIR="${PREK_HOME}/tools/python" \
  uv python install --no-bin --quiet "${PYTHON_VERSION}"

mkdir seed
cd seed
git init --quiet .
cp "${input}/seed.json" .pre-commit-config.yaml
prek prepare-hooks --quiet
cd ..

for config in "${input}"/units/*.json; do
  [ -e "${config}" ] || continue
  unit="$(basename "${config}" .json)"
  mkdir -p "clone/${unit}"
  (
    cd "clone/${unit}"
    git init --quiet .
    cp "${config}" .pre-commit-config.yaml
    if prek list > /dev/null 2> list.err; then
      echo "CLONE ${unit} ok"
    else
      echo "CLONE ${unit} failed"
      sed 's/^/  /' list.err >&2
    fi
  )
done

version="$(prek --version)"
echo "PREK ${version#prek }"
echo "ARCH $(uname -m)"
