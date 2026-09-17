// Package D2, spec Tests item 3: `GET /route` on the Worker -- every validation branch, the
// empty-candidates/null-ranking invariant, `did:key:` extraction and ledger join
// (known/unknown/no-ledger), byte-parity of `dids[].ledger` with `/did/{did}` itself,
// `offer_shape` byte-identical to the committed `worker/src/offer-shape.json`, headers, 405 on
// POST, and a rate-limit denial -- against `makeWorker(fixtureIndex[, ledgerFixture], offerShape)`
// (`../src/routes.js`), deliberately NOT `../src/index.js` (see `worker/test/router.test.mjs`'s
// own comment for why: this stays plain-Node, dependency-free).
//
// Uses the SAME compact-ledger fixture `worker/test/router.test.mjs` loads
// (`tests/fixtures/reputation/compact-fixture.json`) and its real `did:key:` tokens, so a
// `dids[].ledger` join here is checked against genuine facts/score data, not a hand-rolled stand-
// in. The lexical index used here is a small, purpose-built fixture (not the shared 40-doc one --
// none of its documents mention a `did:key:` token), containing exactly one document whose
// abstract mentions both a known and an unknown DID.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import path from "node:path";

import { extractDids, makeWorker, parseRouteParams } from "../src/routes.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, "..", "..");

const LEDGER = JSON.parse(
  readFileSync(
    path.join(REPO_ROOT, "tests", "fixtures", "reputation", "compact-fixture.json"),
    "utf-8",
  ),
);
const NON_BURST_DID = Object.keys(LEDGER.non_burst).sort()[0];
const BURST_DID = Object.keys(LEDGER.burst)[0];
// Well-formed, but not one of the 20 synthetic fixture DIDs above -- same literal
// `worker/test/router.test.mjs` uses for the same reason.
const UNKNOWN_DID = "did:key:z6MkfVWRHNeiV99ckgHDmi8HpwMLtir1XsTu9rNCoYdTuizf";

const OFFER_SHAPE = JSON.parse(
  readFileSync(path.join(HERE, "..", "src", "offer-shape.json"), "utf-8"),
);

const DOC_ABSTRACT =
  `model_hash_route_fixture seen from ${NON_BURST_DID} and also ${UNKNOWN_DID} in the room log`;

// A minimal, hand-built `lexical-v1.json`-shaped index: one document whose abstract contains the
// term the tests query for (`model_hash_route_fixture`) plus two DID mentions. `search()`
// (`../src/search.js`) only needs `index.terms[term]` to hold this document's postings for the
// query to find it -- it does not need every token `tokenize()` would independently emit from the
// abstract (see `openagentsearch.lexical.search`'s own "only terms present are scored" rule).
const INDEX = {
  generated_at: "2026-01-01T00:00:00Z",
  db_sha256: "0".repeat(64),
  bm25: { k1: 1.2, b: 0.75 },
  avgdl: 8,
  counts: { docs: 1, terms: 1, postings: 1 },
  docs: [
    {
      sha: "a".repeat(64),
      url: "https://example.test/room-window-1",
      title: "Room window",
      section: "",
      kind: "room",
      len: 8,
      abstract: DOC_ABSTRACT,
    },
  ],
  terms: {
    model_hash_route_fixture: [[0, 1]],
  },
};

function fakeEnv(limitImpl) {
  return { RATE_LIMITER: { limit: limitImpl } };
}

const ALWAYS_ALLOW = fakeEnv(async () => ({ success: true }));

const BASE = "https://openagentsearch.example.workers.dev";

function req(pathAndQuery, { method = "GET", headers } = {}) {
  return new Request(`${BASE}${pathAndQuery}`, { method, headers });
}

// --- parseRouteParams: validation branches --------------------------------------------------

test("parseRouteParams: missing model_hash", () => {
  const result = parseRouteParams(new URLSearchParams(""));
  assert.deepEqual(result, { error: "missing_model_hash", field: "model_hash" });
});

for (const bad of ["", "a".repeat(129), "has space", "has$dollar"]) {
  test(`parseRouteParams: invalid_model_hash for ${JSON.stringify(bad)}`, () => {
    const result = parseRouteParams(new URLSearchParams({ model_hash: bad }));
    assert.deepEqual(result, { error: "invalid_model_hash", field: "model_hash" });
  });
}

test("parseRouteParams: model_hash at exactly 128 characters is accepted", () => {
  const result = parseRouteParams(new URLSearchParams({ model_hash: "a".repeat(128) }));
  assert.equal(result.model_hash, "a".repeat(128));
});

test("parseRouteParams: precision is optional and not checked against a vocabulary", () => {
  const result = parseRouteParams(
    new URLSearchParams({ model_hash: "m1", precision: "not-a-real-precision-kind" }),
  );
  assert.equal(result.precision, "not-a-real-precision-kind");
});

for (const bad of ["", "a".repeat(33), "bad space"]) {
  test(`parseRouteParams: invalid_precision for ${JSON.stringify(bad)}`, () => {
    const result = parseRouteParams(new URLSearchParams({ model_hash: "m1", precision: bad }));
    assert.deepEqual(result, { error: "invalid_precision", field: "precision" });
  });
}

for (const bad of ["", "abc", "-1", "1.5", "0", "600001"]) {
  test(`parseRouteParams: invalid_max_latency_ms for ${JSON.stringify(bad)}`, () => {
    const result = parseRouteParams(
      new URLSearchParams({ model_hash: "m1", max_latency_ms: bad }),
    );
    assert.deepEqual(result, { error: "invalid_max_latency_ms", field: "max_latency_ms" });
  });
}

test("parseRouteParams: max_latency_ms bounds 1 and 600000 accepted", () => {
  for (const value of ["1", "600000"]) {
    const result = parseRouteParams(
      new URLSearchParams({ model_hash: "m1", max_latency_ms: value }),
    );
    assert.equal(result.max_latency_ms, Number(value));
  }
});

for (const bad of ["", "abc", "-1", "1.5", "0", "51", "０"]) {
  test(`parseRouteParams: invalid_k for ${JSON.stringify(bad)}`, () => {
    const result = parseRouteParams(new URLSearchParams({ model_hash: "m1", k: bad }));
    assert.deepEqual(result, { error: "invalid_k", field: "k" });
  });
}

test("parseRouteParams: k defaults to 10, bounds 1 and 50 accepted", () => {
  const defaulted = parseRouteParams(new URLSearchParams({ model_hash: "m1" }));
  assert.equal(defaulted.k, 10);
  for (const value of ["1", "50"]) {
    const result = parseRouteParams(new URLSearchParams({ model_hash: "m1", k: value }));
    assert.equal(result.k, Number(value));
  }
});

test("parseRouteParams: repeated parameters -- the first value wins", () => {
  const params = new URLSearchParams();
  params.append("model_hash", "first");
  params.append("model_hash", "second");
  const result = parseRouteParams(params);
  assert.equal(result.model_hash, "first");
});

test("parseRouteParams: validation order is model_hash, precision, max_latency_ms, k", () => {
  const result = parseRouteParams(
    new URLSearchParams({
      model_hash: "m1",
      precision: "bad space",
      max_latency_ms: "nope",
      k: "nope",
    }),
  );
  assert.deepEqual(result, { error: "invalid_precision", field: "precision" });
});

// --- extractDids ---------------------------------------------------------------------------

test("extractDids: order of first appearance, deduplicated, capped at 5", () => {
  const text = `first ${BURST_DID} then ${NON_BURST_DID} then ${BURST_DID} again`;
  assert.deepEqual(extractDids(text), [BURST_DID, NON_BURST_DID]);
});

test("extractDids: empty when none present", () => {
  assert.deepEqual(extractDids("nothing to see here"), []);
});

// --- GET /route: offer_shape_missing fail-closed --------------------------------------------

test("GET /route with no offerShape loaded is 500 offer_shape_missing", async () => {
  const worker = makeWorker(INDEX, LEDGER); // offerShape omitted -> null
  const res = await worker.fetch(req("/route?model_hash=m1"), ALWAYS_ALLOW);
  assert.equal(res.status, 500);
  assert.deepEqual(await res.json(), { error: "offer_shape_missing" });
  // docs/api.md: `/route` error responses are cached like `/search` (60 s), not like the card.
  assert.equal(res.headers.get("cache-control"), "public, max-age=60");
});

// --- GET /route: validation branches over HTTP ----------------------------------------------

test("GET /route missing model_hash is 400 missing_model_hash", async () => {
  const worker = makeWorker(INDEX, LEDGER, OFFER_SHAPE);
  const res = await worker.fetch(req("/route"), ALWAYS_ALLOW);
  assert.equal(res.status, 400);
  assert.equal(res.headers.get("cache-control"), "public, max-age=60"); // like /search
  // Present even for a 400 -- a ledger IS loaded, so there is something to report (the same
  // convention GET /did/{did} uses).
  assert.equal(res.headers.get("x-ledger-generated-at"), LEDGER.generated_at);
  assert.deepEqual(await res.json(), { error: "missing_model_hash", field: "model_hash" });
});

test("GET /route: X-Ledger-Generated-At is absent from a 400 when no ledger is loaded", async () => {
  const worker = makeWorker(INDEX, null, OFFER_SHAPE);
  const res = await worker.fetch(req("/route"), ALWAYS_ALLOW);
  assert.equal(res.status, 400);
  assert.equal(res.headers.get("x-ledger-generated-at"), null);
});

test("GET /route: X-Ledger-Generated-At is present even on the 500 offer_shape_missing", async () => {
  const worker = makeWorker(INDEX, LEDGER); // offerShape omitted -> null
  const res = await worker.fetch(req("/route?model_hash=m1"), ALWAYS_ALLOW);
  assert.equal(res.status, 500);
  assert.equal(res.headers.get("x-ledger-generated-at"), LEDGER.generated_at);
});

test("GET /route invalid model_hash is 400 invalid_model_hash", async () => {
  const worker = makeWorker(INDEX, LEDGER, OFFER_SHAPE);
  const res = await worker.fetch(req("/route?model_hash=has%20space"), ALWAYS_ALLOW);
  assert.equal(res.status, 400);
  assert.deepEqual(await res.json(), { error: "invalid_model_hash", field: "model_hash" });
});

test("GET /route invalid precision is 400 invalid_precision", async () => {
  const worker = makeWorker(INDEX, LEDGER, OFFER_SHAPE);
  const res = await worker.fetch(
    req("/route?model_hash=m1&precision=" + encodeURIComponent("bad space")),
    ALWAYS_ALLOW,
  );
  assert.equal(res.status, 400);
  assert.deepEqual(await res.json(), { error: "invalid_precision", field: "precision" });
});

test("GET /route invalid max_latency_ms is 400 invalid_max_latency_ms", async () => {
  const worker = makeWorker(INDEX, LEDGER, OFFER_SHAPE);
  const res = await worker.fetch(req("/route?model_hash=m1&max_latency_ms=abc"), ALWAYS_ALLOW);
  assert.equal(res.status, 400);
  assert.deepEqual(await res.json(), { error: "invalid_max_latency_ms", field: "max_latency_ms" });
});

test("GET /route invalid k is 400 invalid_k", async () => {
  const worker = makeWorker(INDEX, LEDGER, OFFER_SHAPE);
  const res = await worker.fetch(req("/route?model_hash=m1&k=51"), ALWAYS_ALLOW);
  assert.equal(res.status, 400);
  assert.deepEqual(await res.json(), { error: "invalid_k", field: "k" });
});

// --- GET /route: the invariant, offer_shape parity, observations, DID join ------------------

test("GET /route: candidates always [], ranking always null, offer_shape byte-identical", async () => {
  const worker = makeWorker(INDEX, LEDGER, OFFER_SHAPE);
  const res = await worker.fetch(req("/route?model_hash=model_hash_route_fixture"), ALWAYS_ALLOW);
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("cache-control"), "public, max-age=60");
  assert.equal(res.headers.get("x-ledger-generated-at"), LEDGER.generated_at);
  const body = await res.json();
  assert.deepEqual(body.candidates, []);
  assert.equal(body.ranking, null);
  assert.ok(body.candidates_reason.length > 0);
  assert.ok(body.ranking_reason.length > 0);
  assert.equal(body.advisory, true);
  assert.deepEqual(body.offer_shape, OFFER_SHAPE);
  // Order-sensitive: the documented order (`published, source, watch, binds`) is what the A2
  // server emits; the generated file's keys are sorted, so a verbatim splat would differ on the wire.
  assert.deepEqual(Object.keys(body.offer_shape), ["published", "source", "watch", "binds"]);
  assert.equal(body.index_generated_at, INDEX.generated_at);
  assert.equal(body.ledger_generated_at, LEDGER.generated_at);
  assert.equal(body.observations_query, "model_hash_route_fixture");
});

test("GET /route: query object echoes every field, max_latency_ms unused", async () => {
  const worker = makeWorker(INDEX, LEDGER, OFFER_SHAPE);
  const res = await worker.fetch(
    req("/route?model_hash=model_hash_route_fixture&precision=fp16&max_latency_ms=1500&k=3"),
    ALWAYS_ALLOW,
  );
  const body = await res.json();
  assert.deepEqual(body.query, {
    model_hash: "model_hash_route_fixture",
    precision: "fp16",
    max_latency_ms: 1500,
    k: 3,
  });
  assert.equal(body.observations_query, "model_hash_route_fixture fp16");
});

test("GET /route: an observation joins a known DID and an unknown DID to the ledger", async () => {
  const worker = makeWorker(INDEX, LEDGER, OFFER_SHAPE);
  const res = await worker.fetch(req("/route?model_hash=model_hash_route_fixture"), ALWAYS_ALLOW);
  const body = await res.json();
  assert.equal(body.observations.length, 1);
  const observation = body.observations[0];
  assert.equal(observation.url, INDEX.docs[0].url);
  assert.equal(observation.kind, "room");
  assert.equal(observation.text, DOC_ABSTRACT);

  const byDid = Object.fromEntries(observation.dids.map((d) => [d.did, d.ledger]));
  assert.deepEqual(Object.keys(byDid).sort(), [NON_BURST_DID, UNKNOWN_DID].sort());
  assert.equal(byDid[NON_BURST_DID].did, NON_BURST_DID);
  assert.equal(byDid[NON_BURST_DID].burst, false);
  assert.deepEqual(byDid[UNKNOWN_DID], { error: "unknown_did" });
});

test("GET /route: dids[].ledger is byte-identical to GET /did/{did}", async () => {
  const worker = makeWorker(INDEX, LEDGER, OFFER_SHAPE);
  const routeRes = await worker.fetch(
    req("/route?model_hash=model_hash_route_fixture"),
    ALWAYS_ALLOW,
  );
  const routeBody = await routeRes.json();
  const observedLedgerBody = routeBody.observations[0].dids.find(
    (d) => d.did === NON_BURST_DID,
  ).ledger;

  const didRes = await worker.fetch(req(`/did/${NON_BURST_DID}`), ALWAYS_ALLOW);
  const didBody = await didRes.json();

  assert.deepEqual(observedLedgerBody, didBody);
});

test("GET /route: burst DID mention also joins correctly", async () => {
  const burstIndex = {
    ...INDEX,
    docs: [{ ...INDEX.docs[0], abstract: `model_hash_route_fixture posted by ${BURST_DID}` }],
  };
  const worker = makeWorker(burstIndex, LEDGER, OFFER_SHAPE);
  const routeRes = await worker.fetch(
    req("/route?model_hash=model_hash_route_fixture"),
    ALWAYS_ALLOW,
  );
  const routeBody = await routeRes.json();
  const observedLedgerBody = routeBody.observations[0].dids[0].ledger;

  const didRes = await worker.fetch(req(`/did/${BURST_DID}`), ALWAYS_ALLOW);
  const didBody = await didRes.json();

  assert.deepEqual(observedLedgerBody, didBody);
  assert.equal(observedLedgerBody.burst, true);
});

test("GET /route: no ledger loaded answers ledger_not_built for every DID mention", async () => {
  const worker = makeWorker(INDEX, null, OFFER_SHAPE);
  const res = await worker.fetch(req("/route?model_hash=model_hash_route_fixture"), ALWAYS_ALLOW);
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("x-ledger-generated-at"), null); // nothing to report -- no ledger
  const body = await res.json();
  assert.equal(body.ledger_generated_at, null);
  assert.ok(body.observations[0].dids.length > 0);
  for (const entry of body.observations[0].dids) {
    assert.deepEqual(entry.ledger, { error: "ledger_not_built" });
  }
});

test("GET /route: no matching observations is still a 200 with an empty list", async () => {
  const worker = makeWorker(INDEX, LEDGER, OFFER_SHAPE);
  const res = await worker.fetch(
    req("/route?model_hash=completely_unmatched_token_zzz"),
    ALWAYS_ALLOW,
  );
  assert.equal(res.status, 200);
  const body = await res.json();
  assert.deepEqual(body.observations, []);
  assert.deepEqual(body.candidates, []);
  assert.equal(body.ranking, null);
});

// --- HEAD / POST / rate limiting -------------------------------------------------------------

test("HEAD /route carries the same status and headers with no body", async () => {
  const worker = makeWorker(INDEX, LEDGER, OFFER_SHAPE);
  const res = await worker.fetch(
    req("/route?model_hash=model_hash_route_fixture", { method: "HEAD" }),
    ALWAYS_ALLOW,
  );
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("x-ledger-generated-at"), LEDGER.generated_at);
  assert.equal(await res.text(), "");
});

test("POST /route is 405 with Allow: GET, HEAD", async () => {
  const worker = makeWorker(INDEX, LEDGER, OFFER_SHAPE);
  const res = await worker.fetch(
    req("/route?model_hash=m1", { method: "POST" }),
    ALWAYS_ALLOW,
  );
  assert.equal(res.status, 405);
  assert.equal(res.headers.get("allow"), "GET, HEAD");
});

test("GET /route is rate-limited like every other non-exempt route", async () => {
  const worker = makeWorker(INDEX, LEDGER, OFFER_SHAPE);
  const refusing = fakeEnv(async () => ({ success: false }));
  const res = await worker.fetch(req("/route?model_hash=m1"), refusing);
  assert.equal(res.status, 429);
  assert.equal(res.headers.get("retry-after"), "60");
  assert.deepEqual(await res.json(), { error: "rate_limited" });
});

test("GET /route: missing RATE_LIMITER binding is 503 rate_limiter_unavailable", async () => {
  const worker = makeWorker(INDEX, LEDGER, OFFER_SHAPE);
  const res = await worker.fetch(req("/route?model_hash=m1"), {});
  assert.equal(res.status, 503);
  assert.deepEqual(await res.json(), { error: "rate_limiter_unavailable" });
});
