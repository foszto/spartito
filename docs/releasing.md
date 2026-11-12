# Releasing

Spartito is published on PyPI as `spartito`; the import package is `orchestrator`. Releases
are built and uploaded by GitHub Actions with
[Trusted Publishing](https://docs.pypi.org/trusted-publishers/): no token or password is
stored anywhere.

- [The version](#the-version)
- [One-time setup](#one-time-setup)
- [Dry run on TestPyPI](#dry-run-on-testpypi)
- [Cutting a release](#cutting-a-release)
- [Existing installs from a checkout](#existing-installs-from-a-checkout)

## The version

The version lives in one place, `version` in `pyproject.toml`. The package reads it from the
installed metadata (`orchestrator.__version__`, which `/api/ping` and the launcher report).
Bump it with:

```sh
uv version 0.2.0          # or: uv version --bump minor
```

This also updates `uv.lock`. An environment that installed the checkout before the bump
reports the old version until it is installed again (`uv sync`, or the `uv tool install` line
you used).

## One-time setup

**PyPI** (<https://pypi.org/manage/account/publishing/>), a pending trusted publisher:

| field | value |
|---|---|
| PyPI project name | `spartito` |
| owner | `foszto` |
| repository name | `spartito` |
| workflow name | `release.yml` |
| environment name | `pypi` |

**TestPyPI** (<https://test.pypi.org/manage/account/publishing/>, a separate account): the
same, with the environment `testpypi`.

The workflow file name and the environment are part of the publisher's identity: renaming
`.github/workflows/release.yml` or its environments, or moving the publishing job into a
reusable workflow (not supported as a publisher), stops publishing until the publisher is
changed to match. A pending publisher turns into a normal one with the first upload, which
creates the project.

**GitHub**, under the repository's Settings › Environments:

- `pypi`: add yourself as a required reviewer, so that every publish waits for your approval.
  Optionally, under Deployment branches and tags, allow only tags matching `v*`.
- `testpypi`: no rules needed.

No secrets are needed.

## Dry run on TestPyPI

Under Actions › release › Run workflow, choose `master` (or another branch) and run it. The
button appears once `release.yml` is on the default branch. The
`build` job builds the sdist and the wheel and installs the wheel once to run
`spartito --help`. The `testpypi` job then uploads both files to TestPyPI. A run started this
way has no job that publishes to PyPI.

Check the result in a temporary tool directory, so nothing replaces your own install:

```sh
export UV_TOOL_DIR="$(mktemp -d)" UV_TOOL_BIN_DIR="$(mktemp -d)" ORCH_HOME="$(mktemp -d)"
uv tool install --python 3.14 --index https://test.pypi.org/simple/ \
    --index-strategy unsafe-best-match 'spartito[app,menubar]'
"$UV_TOOL_BIN_DIR/spartito" --help
```

`--index-strategy unsafe-best-match` lets uv take the dependencies from PyPI, because
TestPyPI does not have them all. It takes the highest version from either index, and anyone
can upload to TestPyPI under a dependency's name, so use it only for this check.

An index accepts a file name only once. Deleting a release does not free its version for a
new upload. To dry-run again after an upload, bump the version first, for example to a
pre-release such as `0.1.1rc1` (`uv version 0.1.1rc1`). Do this on a branch that you do not
tag; the same rule applies to PyPI.

## Cutting a release

1. On `master`, with the checks green: `uv version <new>` (see [The version](#the-version)),
   commit `pyproject.toml` and `uv.lock`, and push the commit to `master`.
2. Tag the commit and push the tag:

   ```sh
   git tag -a v0.2.0 -m v0.2.0
   git push origin v0.2.0
   ```

3. The tag starts the `release` workflow. `build` checks that the tagged commit is on
   `master`, builds the sdist and the wheel, and checks that the tag is `v` plus the built
   version; either failure stops the run before anything is uploaded. It also installs the wheel once. `pypi` then waits for your approval of the
   `pypi` environment, and uploads both files with
   [digital attestations](https://docs.pypi.org/attestations/).
4. Verify on <https://pypi.org/project/spartito/>, and in a temporary tool directory as
   above, without the TestPyPI options:
   `uv tool install --python 3.14 'spartito[app,menubar]'`. With a temporary
   `ORCH_HOME`, `spartito web --port 18926` serves `/api/ping` with the new version.

A failed upload can be retried by re-running the failed job. Files already on PyPI that are
identical are skipped. A wrong version cannot be uploaded again: release the next one.

## Existing installs from a checkout

Until the distribution was renamed, a checkout installed itself as the distribution
`orchestrator`, and `uv tool install --editable` put it in the tool `orchestrator`. Now it
installs as `spartito`, in a new tool directory. The steps for an existing install are in
[usage.md](usage.md#upgrading-to-the-distribution-name-spartito).
