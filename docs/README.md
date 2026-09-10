# Hexera documentation

This directory is the manual. The [root README](../README.md) is the introduction; everything that
needs more than a paragraph lives here.

Each fact has one home. Where two documents need the same fact, the second links to the first
rather than repeating it, so if a setting's default changes, exactly one file is wrong.

## Reader paths

**I want to run it for the first time.**
[Setup](getting-started/setup.md) is the one authoritative path from a fresh clone, covering credentials and
provisioning → then [Configuration](reference/configuration.md) for every setting.

**I am developing on it.**
[Repository map](architecture/repository-map.md) → [Architecture](architecture/overview.md) →
[Development](development/overview.md) → [Gates](development/gates.md).

**I am working on the browser console.**
[The front door](architecture/overview.md#the-front-door) for what the console is and what it may
not do → [Environments and delivery](deployment/environments-and-delivery.md) for how it is
deployed and which settings reach it.

**I am provisioning the cloud mesh tier, or running the stack for other people.**
[Setup](getting-started/setup.md) → [Deployment](deployment/overview.md) →
[Operating modes](architecture/operating-modes.md) → [Security and privacy](deployment/security-and-privacy.md).

**I am adding or changing a meshing engine.**
[Engine overview](engines/overview.md) → the existing engine nearest to yours →
[Development](development/overview.md) for the native test tiers.

**I am operating a running deployment.**
[Deployment](deployment/overview.md) for lifecycle, health and recovery → [Troubleshooting](getting-started/troubleshooting.md) →
[Configuration](reference/configuration.md) for what a setting will do →
[Security and privacy](deployment/security-and-privacy.md) for retention and deletion.

**I want to understand the design.**
[Architecture](architecture/overview.md) → [Agents](architecture/agents.md) →
[Engine overview](engines/overview.md) → [Policy versions](architecture/overview.md#policy-versions).

## Contents

Every document lives in the folder that owns its subject. Nothing sits loose at the top level.

### `getting-started/`: install it and get past the first failure
| Document | Covers |
|---|---|
| [getting-started/setup.md](getting-started/setup.md) | **the setup authority**: prerequisites, credentials, the two mesh-executor paths, first run, reset |
| [getting-started/troubleshooting.md](getting-started/troubleshooting.md) | local and Cloud Run symptoms, the submission contract, and what to do |

### `architecture/`: how the system is put together
| Document | Covers |
|---|---|
| [architecture/overview.md](architecture/overview.md) | topology, pipeline lifecycle, persistence, events, storage, fencing, policy versions |
| [architecture/agents.md](architecture/agents.md) | every agent role, its state ownership and its transitions |
| [architecture/repository-map.md](architecture/repository-map.md) | package ownership, boundaries and dependency direction |
| [architecture/operating-modes.md](architecture/operating-modes.md) | mesh execution, optional services, data collection and public trace |
| [architecture/context-management.md](architecture/context-management.md) | how each agent handles a growing conversation: the builder's knowledge-block checkpoint, and why the other two need none |

### `engines/`: the five meshing engines
| Document | Covers |
|---|---|
| [engines/overview.md](engines/overview.md) | the shared contract, registry, gates, review and packaging |
| [engines/cfmesh.md](engines/cfmesh.md) | Cartesian OpenFOAM meshes, 2D and 3D |
| [engines/snappy.md](engines/snappy.md) | body-fitted OpenFOAM meshes with prism layers |
| [engines/gmsh.md](engines/gmsh.md) | FEA decks and CFD domains from solid CAD |
| [engines/snappy-multiregion.md](engines/snappy-multiregion.md) | coupled multi-region cases for conjugate heat transfer |
| [engines/vmtk.md](engines/vmtk.md) | vascular internal-flow meshes |

### `development/`: working on the code
| Document | Covers |
|---|---|
| [development/overview.md](development/overview.md) | environment, test tiers, static checks, devtools, fixtures |
| [development/gates.md](development/gates.md) | the five gates and what each one proves |
| [development/release-policy.md](development/release-policy.md) | what makes a commit promotable, and what the release tag must be called |

### `deployment/`: running it for other people
| Document | Covers |
|---|---|
| [deployment/overview.md](deployment/overview.md) | the mesh tier's lifecycle, identities, IAM, health, recovery, submission semantics |
| [deployment/security-and-privacy.md](deployment/security-and-privacy.md) | isolation, redaction, retention, deletion, capture |
| [deployment/admin-console-access.md](deployment/admin-console-access.md) | locking the admin console behind IAP: setup, granting and revoking people, proving it is not public |
| [deployment/console-domains.md](deployment/console-domains.md) | giving the consoles a custom hostname: the load balancer, the DNS step, and why a first run's certificate is `PROVISIONING` by design |
| [deployment/identity-platform.md](deployment/identity-platform.md) | initialising Identity Platform per project, the console's public web config, the sender domain, `CONSOLE_SIGNUP_ENABLED`/`SIGNUP_GRANT_CREDITS`, the `0004` backfill runbook, and retiring `console-auth-users` |

### `reference/`: look a fact up
| Document | Covers |
|---|---|
| [reference/configuration.md](reference/configuration.md) | the authoritative reference for every supported setting |
| [reference/build-inputs.md](reference/build-inputs.md) | every external build input, its lock, and how upgrades are reviewed |
| [reference/third-party.md](reference/third-party.md) | redistributed third-party material and its licences |

## Conventions

- A path in `backtick` form is relative to the repository root unless the sentence says otherwise.
- Commands are written to be run from the repository root.
- Settings are named by their exact environment-variable key.
- No document contains real credentials, and none should be added.
