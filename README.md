# Hexera v0.1

> **Under active development.** Expect rough edges and breaking changes without notice. Not every
> path has been proven end to end, so treat what it produces as provisional and verify anything you
> rely on.

Hexera turns a geometry file and a plain-English description of the job into a solver-ready
simulation mesh, or into a clear explanation of why it could not produce one.

You upload CAD or a surface, say what you want to simulate and which mesher to use. The pipeline
writes that engine's configuration, runs the real mesher, measures the result against the engine's
declared quality gates, and inspects rendered images of it. A mesh is delivered only if it passed.

Hexera is the assistant identity you talk to in the browser. `meshpipeline` is the Python
distribution that implements it, and the name you will see in code, images and package metadata.

Meshing compute is **hybrid**: the local stack is the control plane, and every mesh runs off-box on
a Cloud Run Job you own. You do not install OpenFOAM, cfMesh or VMTK, and there is no local
fallback.

## Documentation

Start at the [documentation index](docs/README.md), which lays out reader paths for new users,
local developers, engine contributors, operators and architecture readers.

To install and run it, follow [Setup](docs/getting-started/setup.md). It is the authority on
prerequisites, credentials, provisioning the mesh executor and the first run.

| Document | What it answers |
|---|---|
| [Setup](docs/getting-started/setup.md) | **start here**: prerequisites, credentials, the two mesh-executor paths, first run |
| [Troubleshooting](docs/getting-started/troubleshooting.md) | a symptom, what it means, and the one thing to do |
| [Configuration](docs/reference/configuration.md) | every supported setting, its default and its consequences |
| [Deployment topology](docs/architecture/operating-modes.md) | local versus self-hosted, what each exposes, mesh dispatch |
| [Architecture](docs/architecture/overview.md) | how the system is put together, and why |
| [Agents](docs/architecture/agents.md) | what each agent owns, and how the graph moves between them |
| [Engines](docs/engines/overview.md) | the engine contract and the five shipped engines |
| [Repository map](docs/architecture/repository-map.md) | which package owns what, and which way dependencies point |
| [Development](docs/development/overview.md) | test tiers, static checks, and how to run them |
| [Deployment](docs/deployment/overview.md) | provisioning and operating the Cloud Run mesh executor |
| [Security and privacy](docs/deployment/security-and-privacy.md) | isolation, redaction, retention and capture |

## On the version number

This is v0.1. Future aspirations include a Version 1.0 which is defined as a release worth charging for: one whose results a stranger could rely on without checking our work, across the cases we claim to support. Hexera is not there yet. It is the bar we are building toward, and the version number will say so on the day it is cleared, not before. :)
