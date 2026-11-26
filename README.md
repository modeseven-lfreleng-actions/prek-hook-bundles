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

The [build workflow](.github/workflows/build.yaml) builds a bundle,
proves it on fresh runners, signs its index and publishes it to the
GitHub Container Registry. This repository's action fetches, verifies
and installs it. No lint job outside this repository calls the action
yet; the design document lists what remains.

## Using a bundle in a lint job

Call the action before the hooks run, on the runner that runs them,
with the prek release the job uses:

<!-- markdownlint-disable MD013 MD046 -->

```yaml
steps:
  - name: "Install prek hook bundle"
    # Pin a real release SHA in place of <SHA>.
    uses: lfreleng-actions/prek-hook-bundles@<SHA>
    with:
      prek_version: "0.4.14"
```

<!-- markdownlint-enable MD013 MD046 -->

The action trusts bundles published from the default branch of the
repository that supplied the action, so the commit a caller pins is
the root of trust. It needs an empty prek store, and it never fails a job
that could have run without it: when no bundle fits, or one fails
verification, prek builds its environments as usual.

### Inputs

<!-- markdownlint-disable MD013 -->

| Name           | Required | Description                                                     | Default                   |
| -------------- | -------- | --------------------------------------------------------------- | ------------------------- |
| prek_version   | True     | The prek release the job runs (X.Y.Z); selects the bundle       |                           |
| config_path    | False    | The hook configuration the job runs                             | `.pre-commit-config.yaml` |
| hooks          | False    | Space or comma separated hook ids to install for; empty for all |                           |
| registry       | False    | Registry path of the bundle; empty for this repository's own    |                           |
| registry_token | False    | Token for a bundle whose package is private                     |                           |
| required       | False    | `true` fails the step unless the bundle installs                | `false`                   |

<!-- markdownlint-enable MD013 -->

### Outputs

<!-- markdownlint-disable MD013 -->

| Name   | Description                                                 |
| ------ | ----------------------------------------------------------- |
| status | `installed`, `not-applicable`, `unverified` or `failed`     |
| report | After an install, a JSON object describing the fetch        |

<!-- markdownlint-enable MD013 -->

## How it fits together

| Stage    | Command    | What it does                                        |
| -------- | ---------- | --------------------------------------------------- |
| Fetch    | `fetch`    | Reads every repository's `.pre-commit-config.yaml`  |
| Plan     | `plan`     | Reduces them to the distinct environments in use    |
| Build    | `build`    | Builds each one in a container of its own           |
| Assemble | `assemble` | Checks every result; writes an index and the blobs  |
| Pull     | `pull`     | Lint job: fetches, verifies and installs a bundle   |

[`bundle.toml`](bundle.toml) names the organisation to survey, the
numbers of repositories that must share an environment before the
builder includes it, and the toolchain versions to build against.

`select` and `install` are the two halves of `pull` after the fetch,
for a bundle that is already on disk.

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
