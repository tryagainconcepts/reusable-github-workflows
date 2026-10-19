# Main CI after automatic merges

The reusable Python workflow merges PRs labelled `automerge` into `main`
using `GITHUB_TOKEN`. It then dispatches the calling workflow on `main`,
because merges made with that token do not trigger push workflows.
No PAT is required for this merge and dispatch path.

Configure the **caller repository's workflow**, on its default branch:

```yaml
name: CI

on:
  pull_request:
  push:
    branches: [main]
  workflow_dispatch:

permissions:
  contents: write
  pull-requests: write
  actions: write

jobs:
  python-shared-workflow:
    uses: tryagainconcepts/reusable-github-workflows/.github/workflows/python-ci.yml@main
    # Retain your existing inputs and secrets.
```

The dispatch targets the caller's workflow ID, obtained from the current run;
the workflow filename does not need to be `main.yml`. Dispatch inputs use their
defaults, so give any required inputs defaults that are appropriate for Main CI.
Keep the job enabled for dispatch events on `main`.

Deployment and release processing require a successful build on `main` and
either a push or a workflow dispatch event. PR runs never deploy, including
after an automatic merge. Dispatch runs cannot auto-merge, preventing a loop.

Manual merges trigger Main CI through `push`; automatic merges trigger it through
`workflow_dispatch`. Repository merge rules still apply. If dispatch fails after
a merge, the dispatch step fails with recovery instructions; run the caller
workflow manually on `main` after fixing its trigger or permissions.

## Direct releases on main

After a successful Main CI build, the direct release job uses Python Semantic
Release 10.7.0 to calculate the next version from Conventional Commits. It updates
static `project.version` / `tool.poetry.version` fields in `pyproject.toml` and
`CHANGELOG.md`, then creates a commit such as:

```text
chore(main): release 0.191.0 [ci skip]
```

The job pushes that commit directly to `main` together with its `v0.191.0` tag
using an atomic, non-forced push. It then creates the GitHub Release. There is no
release PR, release branch, or extra CI dispatch for the release commit. Pushes
use `GITHUB_TOKEN`, so the release commit does not trigger another CI run.
Deployment checks out the exact release commit in the current Main CI run.

Existing `v` version tags remain the version baseline and the existing changelog
is preserved. Features bump minor, fixes/performance changes bump patch, and
breaking changes bump major (minor while below 1.0). Commits that do not request
a release leave the version alone; application deployment can still proceed.
The old Release Please configuration and manifest are no longer used. Custom
extra version files or versioning rules from that configuration need migration
before adopting this workflow. The `release_please_token` secret remains accepted
for caller compatibility but is unused.

Direct writes to `main` must be allowed for `GITHUB_TOKEN`; this workflow does
not bypass repository protection rules. Releases stop if `main` has advanced
since the tested commit. The atomic push also rejects concurrent main changes
without pushing a stray tag. The job keeps its historical `release-please` ID
for dependency compatibility, but is displayed as **Direct release on main**.

If a run fails after pushing the commit/tag but before creating the GitHub
Release, dispatch Main CI on that release commit to retry GitHub Release
creation. Existing GitHub Releases are left intact.
