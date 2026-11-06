# Change management

- Every change reaches `main` through a pull request with review and passing CI.
- Personal environments (`-f slug=<slug>`) are for testing; shared dev deploys from `main`.
- Production deploys only from a `v*` tag, through the GitHub `production` environment, which
  requires approval. The workflow refuses production from a branch.
- Migrations run before the image rolls; each migration states whether it is order-safe.
- Emergency changes follow the same path at speed; a review is recorded afterwards if one was
  skipped.
- Evidence: the PR, its CI run, the deploy workflow run and the approving reviewer — all retained
  by GitHub.
