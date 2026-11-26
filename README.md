<!--
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
-->

# 📦 prek hook bundles

<!-- prettier-ignore-start -->
<!-- markdownlint-disable-next-line MD013 -->
[![Linux Foundation](https://img.shields.io/badge/Linux-Foundation-blue)](https://linuxfoundation.org/) [![Source Code](https://img.shields.io/badge/GitHub-100000?logo=github&logoColor=white&color=blue)](https://github.com/lfreleng-actions/prek-hook-bundles) [![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0) [![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/lfreleng-actions/prek-hook-bundles/badge)](https://scorecard.dev/viewer/?uri=github.com/lfreleng-actions/prek-hook-bundles)
<!-- prettier-ignore-end -->

Prebuilt [prek](https://github.com/j178/prek) hook environments for the
linting that runs on every pull request in the `lfreleng-actions`
organisation.

Most repositories in the organisation run the same hooks at the same
revisions. Each lint job still builds their environments from scratch,
because a pull request's cache is private to that pull request and no
cache crosses a repository boundary. This repository builds each
environment once, in a sandbox, and packages the results so that a lint
job can install the ones its hooks need instead of building them.

[docs/DESIGN.md](docs/DESIGN.md) holds the reasoning, the security
model and the measurements.

## Status

The builder and the installer work, and the
[build workflow](.github/workflows/build.yaml) proves a bundle on
fresh runners each time it runs. The workflow publishes nothing yet,
and no lint job uses a bundle: signing, publishing and the consumer
side are the next stages, described in the design document.

## How it fits together

| Stage    | Command    | What it does                                        |
| -------- | ---------- | --------------------------------------------------- |
| Fetch    | `fetch`    | Reads every repository's `.pre-commit-config.yaml`  |
| Plan     | `plan`     | Reduces them to the distinct environments in use    |
| Build    | `build`    | Builds each one in a container of its own           |
| Assemble | `assemble` | Checks every result; writes an index and the blobs  |
| Select   | `select`   | Lint job: picks the blobs its configuration needs   |
| Install  | `install`  | Lint job: writes those blobs into an empty store    |

[`bundle.toml`](bundle.toml) names the organisation to survey, the
number of repositories that must share an environment before the
builder includes it, and the toolchain versions to build against.

## Running it locally

The build needs Docker, [uv](https://docs.astral.sh/uv/), `zstd` and an
authenticated `gh`:

```bash
uv run --locked python -m hookbundles fetch --out /tmp/configs.jsonl
uv run --locked python -m hookbundles plan \
  --configs /tmp/configs.jsonl --out /tmp/plan.json
uv run --locked python -m hookbundles build \
  --plan /tmp/plan.json --work /tmp/work
uv run --locked python -m hookbundles assemble \
  --plan /tmp/plan.json --work /tmp/work --out /tmp/bundle
```

The result is `/tmp/bundle/index.json` and `/tmp/bundle/blobs/`. A
bundle serves the machine architecture Docker runs, so one built on an
Apple Silicon laptop serves `aarch64` Linux and not a hosted runner.

Run the tests, which need neither Docker nor a network:

```bash
uv run --locked python -m unittest discover -s tests -t .
```
