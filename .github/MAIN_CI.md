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

The optional `release_please_token` still controls whether newly created release
PRs trigger their own CI. Without it, Release Please uses `GITHUB_TOKEN` and those
release PRs need a manual merge (or a separate mechanism to start their checks).
