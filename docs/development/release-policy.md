# Release policy

One question this document answers: **what makes a commit releasable, and what is the release
called?** The gates ([development/gates.md](gates.md)) prove a commit is *sound*. This
policy decides whether a sound commit may be *promoted*, and under what name.

Enforced by `devtools/release/tag_policy.py`. Nothing here is advisory.

## The version has exactly one home

`src/meshpipeline/__init__.py` carries `__version__`. `pyproject.toml` derives from it
(`version = { attr = "meshpipeline.__version__" }`), the Docker build receives it as `APP_VERSION`,
and the OCI `org.opencontainers.image.version` label repeats it. Every one of those is a copy; the
package metadata is the original.

**A branch name is not a version.** `release/0.1` is the working name of a *candidate*. Anyone can
rename a branch, and nothing about that act should be able to change what a build claims to be. A
checkout is whatever `__version__` says it is, whatever the branch is called.

## The tag

| Rule | Why |
|---|---|
| The tag is `v<package-version>`, currently `v0.1` | A release is a point; a name with variants is a name that needs explaining. This is a convention the person tagging follows: nothing parses, compares or rejects a version string |
| The tag is **annotated**, never lightweight | An annotated tag is an object with an author, a date and a message. A lightweight tag is a bare pointer anyone can move without trace |
| The tag targets the exact validated promotion commit | Not its parent, not a rebuilt equivalent |
| The promotion commit's tree is clean | A dirty tree means the validated bytes are not the committed bytes |
| Published tags are **immutable** | Moving or reusing one silently changes what a version means for everyone who already fetched it |

There is no `versions/` directory and none is part of this authority.

## Validation creates nothing

Readiness validation is a **read**. It runs `git status`, `git rev-parse`, `git cat-file` and
`git for-each-ref`, and it refuses by construction to run anything that writes a ref or names a
remote. It creates no tag, publishes nothing, and reports every reason a commit is not promotable in
one pass rather than the first.

It also does not require network access. A machine with no route to a remote can establish local
readiness completely; only the final push needs connectivity, and that is a separate act.

## Pushing is a separate: human-authorized action

Nothing in this repository pushes a branch, pushes a tag, publishes a wheel or creates a release.
The only remote-capable command is `docker push` inside `devtools/release/publish.sh` (Gate C part
two), which an operator runs deliberately. Creating the tag and pushing it are steps a person takes,
with intent, after the gates are green.

## Where this repository stands

`release/0.1` is an **untagged release candidate**, and a candidate is not promotable until a
tag matching its package version exists and the gates have passed. That is deliberate: the version
bump belongs to the promotion itself, not to the work leading up to it. Run
`tag_policy.readiness("v0.1")` to see what a given tag would still refuse, and read the
current version from `src/meshpipeline/__init__.py`.

## Promoting a candidate: later

These steps are documented, not performed:

1. Finish the testing the candidate exists for.
2. Set `__version__` to the new version in `src/meshpipeline/__init__.py`, the one edit that makes
   the product a new version.
3. Rebuild and rerun the complete gates against that commit, so the artifacts carry the new version
   in their labels and the record proves it.
4. Create the approved promotion commit on whatever mainline policy is in force.
5. Create the annotated tag locally: `git tag -a v$VERSION -m "v$VERSION"`, on that exact commit,
   where `$VERSION` is what `src/meshpipeline/__init__.py` declares.
6. Push, branch and tag, only after separate, explicit authorization.

Step 6 is the first moment anything leaves the machine.
