# CI and release workflow

Two GitHub Actions workflows live in `.github/workflows/`:

| Workflow      | Runs on                                | Does                                            |
| ------------- | -------------------------------------- | ----------------------------------------------- |
| `ci.yml`      | push to any branch, pull requests      | ruff lint, ruff format check, ty, pytest        |
| `release.yml` | push of a `v*` tag                     | CI checks, wheel build, GitHub Release          |

## CI

`ci.yml` runs one job on `ubuntu-latest`. GitHub has no Rocky 9 runners,
and Python 3.11 on Ubuntu is close enough to EL9's `python3.11` for unit
tests. If that changes, the job can run inside a `rockylinux:9` container.

The job:

1. installs uv and Python from `.python-version` (3.11);
2. runs `uv sync --locked`, so CI fails if `uv.lock` is out of date;
3. runs `uv run ruff check`, `uv run ruff format --check`, `uv run ty check`
   and `uv run pytest`.

The integration tests skip themselves because `SCIOC_TARGET` is not set.
Run them locally (`SCIOC_TARGET=container`) or on the test VM
(`SCIOC_TARGET=vm`).

On a pull request from a branch in this repo the checks run twice, once for
the branch push and once for the pull request.

## Releases

Releases are made by pushing a tag. There is no release PR or extra tooling.

`release.yml`:

1. runs the CI checks (it calls `ci.yml`);
2. fails if the tag is not `v` plus the version in `pyproject.toml`;
3. builds the wheel with `uv build --wheel --no-sources`. Only the
   `service_control_ioc` package goes in; the tests and the `teststack`
   workspace member do not;
4. creates a published GitHub Release named after the tag, with the wheel
   attached and release notes generated from the commits and pull requests
   since the previous release.

### Making a release

1. Bump the version in `pyproject.toml`:

   ```bash
   uv version --bump minor   # or major / patch
   ```

2. Commit the change and get it onto `main` (through a pull request if
   `main` is protected).
3. Tag the commit on `main` and push the tag:

   ```bash
   git switch main && git pull
   git tag -a v0.2.0 -m "v0.2.0"
   git push origin v0.2.0
   ```

4. Watch the Release workflow in the Actions tab. When it finishes, the
   release and its wheel are on the Releases page.

If the tag does not match `pyproject.toml`, the workflow fails before it
publishes anything. Delete the tag (`git push origin :refs/tags/v0.2.0`),
fix the version and tag again. If the tag ruleset below blocks deletion,
this needs someone on the ruleset's bypass list, or use the next patch
version instead.

### Installing a release

Download the wheel from the release and install it on the target:

```bash
uv pip install service_control_ioc-0.2.0-py3-none-any.whl
```

## Repository settings

These are set in the GitHub repository settings, not in the workflows:

1. **Settings → Actions → General → Actions permissions:** allow actions
   created by GitHub plus `astral-sh/setup-uv@*`. If the organisation
   restricts actions, an org admin must allow them there too.
2. **Workflow permissions:** leave the default token read-only. The release
   job asks for `contents: write` itself.
3. **Settings → Rules → Rulesets, branch rule for `main`:** require a pull
   request and the `check` status check. The check only appears in the list
   after CI has run once.
4. **Tag rule for `v*`:** restrict who can create tags and block updates and
   deletions, so a release tag cannot be moved.
5. **Optional, Settings → General → Releases:** turn on immutable releases
   so published assets cannot be replaced. The workflow attaches the wheel
   when it creates the release, which works with this.
6. **Fork pull requests:** keep "require approval for outside contributors"
   on (the default).
