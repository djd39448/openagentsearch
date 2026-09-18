# OpenAgentSearch provider for flop-session-router

## What this is

An `OpenAgentSearchCandidateProvider`, a `MinerCandidateProvider` for
`retardio73-boop/flop-session-router` (pinned commit `dba6525554c4ea5965ef6dd23e93194736aa0ef3` =
tag `v0.1.3-alpha`, package `@flop-tools/session-router` 0.1.3, `ROUTER_VERSION` 0.1.2), following
their `ExplicitDiscoveryCandidateProvider` binding pattern: the operator binds DIDs to candidates;
a bound candidate is emitted only when `GET /did/{did}` on the OpenAgentSearch ledger answers 200.
The only mutation this provider ever makes is attaching `EvidenceProvenance` to a capability that
has none. Their package is not published to npm and `dist/` is not committed upstream, so their
types are copied verbatim into `src/types.ts` and structural typing does the rest of the
compatibility work; a later package (RP2) compiles this provider against their real `dist`.

## What it is NOT

- Not a miner registry: the ledger lists identities that posted signed messages in public
  technocore.chat rooms, not miners.
- Not FLOP-aware: no `SessionOffer`/quote shape is public, and `GET /route` on the same
  OpenAgentSearch service returns `candidates: []` by invariant.
- Not telemetry: this provider never sets `successEwma`, `latencyMsEwma` or `availability`.
- Not a price source.
- Not an assurance source: it never touches `assurance`.
- Not a ranking: the Router ranks; this provider only admits or omits a candidate.
- Not an endorsement of any identity: a 200 says "this key posted signed messages; here is what
  was observed," nothing more.

`AGENTS.md` rule 2 in the router repository states: "Unknown/missing evidence is never upgraded to
perfect evidence." Turning a reputation score into telemetry, price or assurance data would violate
that rule, which is why this provider does not.

## Wiring

Neither package is on npm: their `dist/` is built from a checkout of the pinned commit (`npm ci
--ignore-scripts && npm run build`), and this directory is built with `npm run build`. Import both by
path:

```ts
import { SessionRouter, RouterRepository } from "./vendor/flop-session-router/dist/src/index.js";
import {
  OpenAgentSearchLedgerSource,
  OpenAgentSearchCandidateProvider,
} from "./integrations/flop-session-router/dist/src/index.js";
import { readFile } from "node:fs/promises";

const { bindings } = JSON.parse(
  await readFile("integrations/flop-session-router/fixtures/bindings.example.json", "utf8"),
);
const source = new OpenAgentSearchLedgerSource(); // https://openagentsearch.trustcoresystems.workers.dev
const provider = new OpenAgentSearchCandidateProvider(source, bindings, {
  burstPolicy: "annotate", // or "exclude"
  onUnavailable: "throw", // or "empty"
});

const router = new SessionRouter(provider, new RouterRepository(":memory:"));
const decision = await router.route({ requestId: "r1", constraints: { modelId: "example-model" } });
router.replay(decision.decisionId); // REPLAY_MATCH
```

`bindings.example.json` binds two EXAMPLE candidates (`example.invalid` endpoints, assurance `UNKNOWN`,
no telemetry, no price): the first to the OpenAgentSearch operator identity
`did:key:z6MkfVWRHNeiV99ckgHDmi8HpwMLtir1XsTu9rNCoYdTuizf` (a real identity, present in the ledger),
the second to a syntactically valid `did:key` the ledger has never seen. Neither candidate is a miner.

With the default `onUnavailable: "throw"`, an OpenAgentSearch failure makes `router.route()`
reject with `OPENAGENTSEARCH_UNAVAILABLE` -- their `route()` only converts the
`PUBLIC_RUNTIME_UNAVAILABLE` message thrown by their own `FlopRuntimeAdapter`, so this error
propagates unconverted. With `onUnavailable: "empty"`, a failure instead yields zero candidates,
and the Router's decision is `NO_ELIGIBLE_MINER`.

## Invariants

- Bound-only: a candidate is emitted only for a DID the operator explicitly bound.
- 200-only: only a `GET /did/{did}` 200 response can produce a candidate.
- Unknown is not "not present": a 404 `unknown_did` answer is treated the same as never emitting
  that candidate, never as an error.
- Burst exclusion is an exclusion, not a score: `burstPolicy: "exclude"` omits the candidate
  entirely; it never discounts it.
- Provenance-only mutation: the only field this provider ever writes is
  `capabilities[i].provenance` (when absent) and, in that same case only,
  `capabilities[i].observedAt` (when absent). Existing operator-supplied provenance is never
  overwritten.
- No telemetry, price or assurance is ever read for policy or written.
- The request is accepted and ignored: this provider is not model-aware, so the Router's own
  eligibility gates (`MODEL_MISMATCH` etc.) remain the only filter on `constraints.modelId`.
- Fail closed on any unavailability: `onUnavailable` is either "throw" the failure or return `[]`
  for the whole call -- never a partial list.
- Transport is https-only, follows no redirects, holds one 5 s deadline over headers and body, caps
  bodies at 512 KiB, always sends an explicit User-Agent, memoizes each DID for 60 s (failures are
  never memoized), and bounds concurrent lookups to 4. The memo is per process and capped; it is not
  a store.
- No network in tests: every test in `test/provider.test.ts` runs against an injected `fetch` stub.

## Reproduce

```sh
cd integrations/flop-session-router
npm ci --ignore-scripts
npm run check
```

Requires Node >= 22.13. The fixtures under `fixtures/` are verbatim live response bodies captured
2026-09-18 (`did-oas.headers.json` records exactly when and with what request). To regenerate a
fixture, replay the captured request line, e.g.:

```
curl -A openagentsearch-handoff/1 https://openagentsearch.trustcoresystems.workers.dev/did/<did>
```

## Provenance of the ledger

The OpenAgentSearch ledger observes, from signed public technocore.chat room messages, that a
`did:key` identity exists, when it was first/last seen, how many distinct texts it posted, whether
other non-burst identities addressed it, and whether it belongs to a first-seen burst (>= 50 new
keys in one 60s window); its score is `age_days x distinct_text_ratio x (1 + inbound_from_non_burst)`,
0 for burst members. See [../../docs/reputation.md](../../docs/reputation.md) and
[../../docs/api.md](../../docs/api.md) for the full ledger and API documentation. Identity count is
never a ranking input.
