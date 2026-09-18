# Pins

What this package's CI (`.github/workflows/flop-session-router-provider.yml`) checks out, builds,
and runs the routing journey (`journey/run.mjs`) against, and what evidence it produces. The
journey uses fixtures only: no network, no wallet, no identity, no key.

## Upstream router

| Field | Value |
| --- | --- |
| Repository | `retardio73-boop/flop-session-router` |
| Commit | `dba6525554c4ea5965ef6dd23e93194736aa0ef3` |
| Tag | `v0.1.3-alpha` |
| Tag object | `436a0b8e5a7b98f6b449d5e900390c21e350eff5` |
| Package version (`package.json`) | `0.1.3` |
| `ROUTER_VERSION` (exported from `src/router.ts`) | `0.1.2` |

The package version and `ROUTER_VERSION` disagree (`0.1.3` vs `0.1.2`). That is recorded as-is,
never "fixed" — it is upstream's own mismatch, not this package's to resolve.

## This package

| Field | Value |
| --- | --- |
| Name | `openagentsearch-flop-session-router-provider` |
| Version | `0.1.0` (see `package.json`) |

## Fixtures (Git blob sha256)

Each hash is `sha256` of the Git blob (`git show <commit>:<path> | sha256sum`, commit `4dbc284b448e2e8bb9883f5e15d11ed784012c03`,
where these files were introduced and have not changed since), never of a working copy, which can
differ from the committed blob under `autocrlf` or other checkout-time rewriting. The files are
marked `-text` in `.gitattributes`, so a checkout yields the same bytes and `journey/out/pins.json`
reports the same values.

| File | Git blob sha256 |
| --- | --- |
| `fixtures/bindings.example.json` | `57bbbdc944cf4e3a00cf935e440e97e37339f77eac0b179be12a4c777ef438f0` |
| `fixtures/did-not-built.json` | `1c7aa972b6ba77ea8e4b4ec823c3e150a356516b69410d820dc3f77e707560b6` |
| `fixtures/did-oas.headers.json` | `c431cb5e24c90b0cf85c9bd01db5c89b12e8ad6197e762f02dc0ed28532b0243` |
| `fixtures/did-oas.json` | `13465333db3e3a7aa0e4190260e1feb48d198e0f38cdcc2c0fd7f099dd2e00d1` |
| `fixtures/did-unknown.json` | `b62db0e4a982f60ceeef4e1ac2e1ea349faccf248a1d1ea56da06bed7be31d74` |
| `fixtures/healthz.json` | `624bc53ac9bb498e155cfb32059c1722fbeca9de20d90cc0c7316117fc2364e4` |

## Workflow and artifact

| Field | Value |
| --- | --- |
| Workflow file | `.github/workflows/flop-session-router-provider.yml` |
| Artifact name | `flop-session-router-journey` |
| Artifact retention | 90 days |
| Artifact contents | `integrations/flop-session-router/journey/out/` |

## What the journey is and is not

The journey (`journey/run.mjs`) drives a real `SessionRouter` from the pinned router's own built
`dist/`, and a real `OpenAgentSearchCandidateProvider` from this package's own built `dist/`,
against an in-process `fetch` stub that answers only from this package's committed fixtures. It
never opens a socket, never touches a wallet, never resolves or verifies a DID's cryptographic
identity, and never handles a key of any kind.

### Reproducible across runs (same fixtures, same pinned commit)

- Every decision's `status` and `ranking`
- `router.replay(...)` returning `REPLAY_MATCH`
- `configHash` (the router runs under the unmodified `DEFAULT_CONFIG`)
- Every fixture's sha256 (`journey/out/journey.json` `fixtures`, and this file's table above)
- The J1-J7 check outcomes themselves

### NOT reproducible across runs

- `decisionId` (the router's own `randomUUID()`)
- `generatedAt` (the real wall-clock time a given run finished)
