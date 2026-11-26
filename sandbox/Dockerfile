# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

# The sandbox every hook environment is built in. See docs/DESIGN.md.
#
# It is the distribution release of the runner image named by
# 'image_os' in bundle.toml, so an environment built here finds the
# same C library on the runner that later uses it. It carries git,
# prek and uv and no language toolchain at all: prek then downloads
# the ones bundle.toml names into its own store, and no environment
# can come to depend on a toolchain that only this image has.
#
# The user and home directory match a GitHub-hosted runner's, because
# hook environments embed the absolute path they were built at.

FROM ghcr.io/astral-sh/uv:0.13.4@sha256:9f130421e930bfbdc563c00740466603f8723158e90297d07957c2294640262b AS uv

# The prek release here is the one a bundle serves: the installer
# refuses a lint job that runs any other.
FROM ghcr.io/j178/prek:0.4.14@sha256:27124ea262fbb865ef8064ceb257b2f000a36f212db564eb9bb28f8ea74deffd AS prek

FROM ubuntu:24.04@sha256:562111b88caa79c71391c0c4d3c2114a7d5c35915c20561fd893cb1e07929e6b

# libatomic1: the Node.js release binaries link against it, and the
# base image leaves it out.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates git libatomic1 \
    && rm -rf /var/lib/apt/lists/* \
    && userdel --remove ubuntu \
    && useradd --create-home --uid 1001 --shell /bin/bash runner

COPY --from=uv /uv /uvx /usr/local/bin/
COPY --from=prek /prek /usr/local/bin/prek
COPY --chmod=0555 base.sh unit.sh /usr/local/bin/

USER runner
ENV HOME=/home/runner
WORKDIR /home/runner/work
