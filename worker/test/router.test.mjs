// Package C2b, spec Tests item 3 (extended by package B2's `/did/{did}` cases): the JSON router
// built with `makeWorker(fixtureIndex[, ledgerFixture])` from `worker/src/routes.js` --
// deliberately NOT `worker/src/index.js`, which imports `agents` and the MCP SDK and therefore
// only runs from WSL (see `worker/test-mcp/mcp.test.mjs`). Every route, every documented status,
// the full header set (successes AND errors), the rate limiter's four outcomes, and the bound
// checks.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import path from "node:path";

import { makeWorker } from "../src/routes.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, "..", "..");
const INDEX = JSON.parse(
  readFileSync(
    path.join(REPO_ROOT, "tests", "fixtures", "lexical", "fixture-index-v1.json"),
    "utf-8",
  ),
);
// Package B2: the compact reputation-ledger fixture, generated from the same 20-DID log package
// B1's Python tests replay (`scripts/make_reputation_fixture.py`, extended). One DID (200 posts
// inside a few minutes) is its one burst member; every other DID is non-burst.
const LEDGER = JSON.parse(
  readFileSync(
    path.join(REPO_ROOT, "tests", "fixtures", "reputation", "compact-fixture.json"),
    "utf-8",
  ),
);
const NON_BURST_DID = Object.keys(LEDGER.non_burst).sort()[0];
const BURST_DID = Object.keys(LEDGER.burst)[0];
// Well-formed, but not one of the 20 synthetic fixture DIDs above.
const UNKNOWN_DID = "did:key:z6MkfVWRHNeiV99ckgHDmi8HpwMLtir1XsTu9rNCoYdTuizf";

const BASE = "https://openagentsearch.example.workers.dev";

function fakeEnv(limitImpl) {
  return { RATE_LIMITER: { limit: limitImpl } };
}

const ALWAYS_ALLOW = fakeEnv(async () => ({ success: true }));

function req(pathAndQuery, { method = "GET", headers } = {}) {
  return new Request(`${BASE}${pathAndQuery}`, { method, headers });
}

const COMMON_HEADER_NAMES = [
  "content-type",
  "access-control-allow-origin",
  "x-content-type-options",
  "x-index-generated-at",
  "x-index-db-sha256",
  "cache-control",
];

function assertCommonHeaders(response) {
  for (const name of COMMON_HEADER_NAMES) {
    assert.ok(response.headers.has(name), `missing header ${name}`);
  }
  assert.equal(response.headers.get("content-type"), "application/json; charset=utf-8");
  assert.equal(response.headers.get("access-control-allow-origin"), "*");
  assert.equal(response.headers.get("x-content-type-options"), "nosniff");
  assert.equal(response.headers.get("x-index-generated-at"), INDEX.generated_at);
  assert.equal(response.headers.get("x-index-db-sha256"), INDEX.db_sha256);
}

// --- GET / -------------------------------------------------------------------------------

test("GET / returns the service card with the full header set", async () => {
  const worker = makeWorker(INDEX);
  const res = await worker.fetch(req("/"), ALWAYS_ALLOW);
  assert.equal(res.status, 200);
  assertCommonHeaders(res);
  assert.equal(res.headers.get("cache-control"), "public, max-age=300");
  const body = await res.json();
  assert.equal(body.generated_at, INDEX.generated_at);
  assert.equal(body.db_sha256, INDEX.db_sha256);
  assert.ok(Array.isArray(body.routes));
  assert.ok(body.routes.includes("POST /mcp"));
  assert.deepEqual(body.tools, ["search", "did_lookup", "index_info", "route"]);
  assert.equal(body.ledger, null); // no ledger passed to makeWorker (package B2)
});

test("GET / reports the ledger counts when a ledger is loaded", async () => {
  const worker = makeWorker(INDEX, LEDGER);
  const res = await worker.fetch(req("/"), ALWAYS_ALLOW);
  const body = await res.json();
  assert.deepEqual(body.ledger, {
    dids: LEDGER.dids,
    bursts: LEDGER.bursts,
    generated_at: LEDGER.generated_at,
  });
});

test("/ bypasses the rate limiter even when the binding would refuse", async () => {
  const worker = makeWorker(INDEX);
  const refusing = fakeEnv(async () => ({ success: false }));
  const res = await worker.fetch(req("/"), refusing);
  assert.equal(res.status, 200);
});

test("/ bypasses the rate limiter even when the binding is missing", async () => {
  const worker = makeWorker(INDEX);
  const res = await worker.fetch(req("/"), {});
  assert.equal(res.status, 200);
});

test("HEAD / returns the same status and headers with no body", async () => {
  const worker = makeWorker(INDEX);
  const res = await worker.fetch(req("/", { method: "HEAD" }), ALWAYS_ALLOW);
  assert.equal(res.status, 200);
  assertCommonHeaders(res);
  const text = await res.text();
  assert.equal(text, "");
});

test("POST / is 405 with Allow: GET, HEAD", async () => {
  const worker = makeWorker(INDEX);
  const res = await worker.fetch(req("/", { method: "POST" }), ALWAYS_ALLOW);
  assert.equal(res.status, 405);
  assert.equal(res.headers.get("allow"), "GET, HEAD");
  assertCommonHeaders(res);
});

// --- GET /healthz --------------------------------------------------------------------------

test("GET /healthz reports index counts and bypasses the rate limiter", async () => {
  const worker = makeWorker(INDEX);
  const refusing = fakeEnv(async () => ({ success: false }));
  const res = await worker.fetch(req("/healthz"), refusing);
  assert.equal(res.status, 200);
  assertCommonHeaders(res);
  const body = await res.json();
  assert.equal(body.status, "ok");
  assert.equal(body.index.indexed, INDEX.counts.docs);
  assert.equal(body.lexical.docs, INDEX.counts.docs);
  assert.equal(body.lexical.terms, INDEX.counts.terms);
  assert.equal(body.lexical.postings, INDEX.counts.postings);
  assert.equal(body.generated_at, INDEX.generated_at);
  assert.equal(body.ledger, null); // no ledger passed to makeWorker (package B2)
});

test("GET /healthz reports the ledger counts when a ledger is loaded", async () => {
  const worker = makeWorker(INDEX, LEDGER);
  const res = await worker.fetch(req("/healthz"), ALWAYS_ALLOW);
  const body = await res.json();
  assert.deepEqual(body.ledger, {
    dids: LEDGER.dids,
    bursts: LEDGER.bursts,
    generated_at: LEDGER.generated_at,
  });
});

// --- GET /search -----------------------------------------------------------------------------

test("GET /search returns results with the search cache duration", async () => {
  const worker = makeWorker(INDEX);
  const res = await worker.fetch(req("/search?q=authentication&k=5"), ALWAYS_ALLOW);
  assert.equal(res.status, 200);
  assertCommonHeaders(res);
  assert.equal(res.headers.get("cache-control"), "public, max-age=60");
  const body = await res.json();
  assert.equal(body.query, "authentication");
  assert.equal(body.k, 5);
  assert.ok(Array.isArray(body.results));
  assert.ok(body.results.length > 0);
  assert.deepEqual(Object.keys(body.results[0]).sort(), [
    "doc_sha256",
    "doc_url",
    "kind",
    "score",
    "section",
    "snippet",
    "title",
  ]);
});

test("GET /search default k is 10", async () => {
  const worker = makeWorker(INDEX);
  const res = await worker.fetch(req("/search?q=error"), ALWAYS_ALLOW);
  const body = await res.json();
  assert.equal(body.k, 10);
});

test("missing q is 400 missing_query", async () => {
  const worker = makeWorker(INDEX);
  const res = await worker.fetch(req("/search"), ALWAYS_ALLOW);
  assert.equal(res.status, 400);
  assertCommonHeaders(res);
  assert.deepEqual(await res.json(), { error: "missing_query" });
});

test("blank q is 400 missing_query", async () => {
  const worker = makeWorker(INDEX);
  const res = await worker.fetch(req(`/search?q=${encodeURIComponent("   ")}`), ALWAYS_ALLOW);
  assert.equal(res.status, 400);
  assert.deepEqual(await res.json(), { error: "missing_query" });
});

test("q over 512 characters is 400 query_too_long", async () => {
  const worker = makeWorker(INDEX);
  const longQ = "a".repeat(513);
  const res = await worker.fetch(req(`/search?q=${longQ}`), ALWAYS_ALLOW);
  assert.equal(res.status, 400);
  assert.deepEqual(await res.json(), { error: "query_too_long" });
});

test("q at exactly 512 characters is accepted", async () => {
  const worker = makeWorker(INDEX);
  const q = "a".repeat(512);
  const res = await worker.fetch(req(`/search?q=${q}`), ALWAYS_ALLOW);
  assert.equal(res.status, 200);
});

for (const badK of ["０", "0", "51", "abc", "-1", "5.5", ""]) {
  test(`invalid_k for k=${JSON.stringify(badK)}`, async () => {
    const worker = makeWorker(INDEX);
    const res = await worker.fetch(
      req(`/search?q=authentication&k=${encodeURIComponent(badK)}`),
      ALWAYS_ALLOW,
    );
    assert.equal(res.status, 400);
    assert.deepEqual(await res.json(), { error: "invalid_k" });
  });
}

test("k=1 and k=50 are both accepted", async () => {
  const worker = makeWorker(INDEX);
  for (const k of [1, 50]) {
    const res = await worker.fetch(req(`/search?q=error&k=${k}`), ALWAYS_ALLOW);
    assert.equal(res.status, 200, `k=${k}`);
  }
});

test("unknown kind is 400 invalid_kind, with the known set in the body", async () => {
  const worker = makeWorker(INDEX);
  const res = await worker.fetch(req("/search?q=error&kind=not_a_real_kind"), ALWAYS_ALLOW);
  assert.equal(res.status, 400);
  assert.deepEqual(await res.json(), {
    error: "invalid_kind",
    known: ["github_doc", "github_issue", "room", "site"],
  });
});

test("known kind filters results", async () => {
  const worker = makeWorker(INDEX);
  const res = await worker.fetch(req("/search?q=integration&kind=site"), ALWAYS_ALLOW);
  assert.equal(res.status, 200);
  const body = await res.json();
  assert.ok(body.results.every((r) => r.kind === "site"));
});

test("kind validity is derived from the loaded index, not a hard-coded list", async () => {
  // A kind present in this index but never hard-coded anywhere (`html`) must be accepted; a kind
  // that used to be hard-coded but is absent from this particular index (`room_message`) must be
  // rejected. Regression test for the "kind list computed at startup from the index" rule in
  // `handoff/C2b-SPEC.md` §3.
  const withHtmlDoc = {
    ...INDEX,
    docs: [{ ...INDEX.docs[0], kind: "html" }, ...INDEX.docs.slice(1)],
  };
  const worker = makeWorker(withHtmlDoc);

  const htmlRes = await worker.fetch(req("/search?q=configuration&kind=html"), ALWAYS_ALLOW);
  assert.equal(htmlRes.status, 200);

  const roomMessageRes = await worker.fetch(
    req("/search?q=configuration&kind=room_message"),
    ALWAYS_ALLOW,
  );
  assert.equal(roomMessageRes.status, 400);
  assert.deepEqual(await roomMessageRes.json(), {
    error: "invalid_kind",
    known: ["github_doc", "github_issue", "html", "room", "site"],
  });
});

test("POST /search is 405", async () => {
  const worker = makeWorker(INDEX);
  const res = await worker.fetch(req("/search?q=x", { method: "POST" }), ALWAYS_ALLOW);
  assert.equal(res.status, 405);
  assert.equal(res.headers.get("allow"), "GET, HEAD");
});

// --- GET /did/{did} ----------------------------------------------------------------------

test("makeWorker(index, null): a well-formed did:key answers 404 ledger_not_built", async () => {
  const worker = makeWorker(INDEX, null);
  const res = await worker.fetch(req(`/did/${UNKNOWN_DID}`), ALWAYS_ALLOW);
  assert.equal(res.status, 404);
  assertCommonHeaders(res);
  assert.deepEqual(await res.json(), { error: "ledger_not_built" });
  assert.equal(res.headers.get("x-ledger-generated-at"), null); // nothing to report -- no ledger
});

test("makeWorker(index): omitting ledger defaults to the same ledger_not_built answer", async () => {
  const worker = makeWorker(INDEX);
  const res = await worker.fetch(req(`/did/${UNKNOWN_DID}`), ALWAYS_ALLOW);
  assert.equal(res.status, 404);
  assert.deepEqual(await res.json(), { error: "ledger_not_built" });
});

test("a malformed did is 400 invalid_did, with no ledger loaded", async () => {
  const worker = makeWorker(INDEX);
  const res = await worker.fetch(req("/did/not-a-valid-did"), ALWAYS_ALLOW);
  assert.equal(res.status, 400);
  assert.deepEqual(await res.json(), { error: "invalid_did" });
});

test("did without the z multibase prefix is 400 invalid_did", async () => {
  const worker = makeWorker(INDEX);
  const res = await worker.fetch(req("/did/did:key:6MkfVWRHNeiV99ckgHDmi8HpwMLtir1XsTu9rNCoYdTuizf"), ALWAYS_ALLOW);
  assert.equal(res.status, 400);
});

// --- GET /did/{did}, with a compact ledger loaded (package B2) -----------------------------

test("a known non-burst did answers 200 with the full facts/score body", async () => {
  const worker = makeWorker(INDEX, LEDGER);
  const res = await worker.fetch(req(`/did/${NON_BURST_DID}`), ALWAYS_ALLOW);
  assert.equal(res.status, 200);
  assertCommonHeaders(res);
  assert.equal(res.headers.get("x-ledger-generated-at"), LEDGER.generated_at);
  const body = await res.json();
  const expectedRow = LEDGER.non_burst[NON_BURST_DID];
  assert.deepEqual(body, {
    did: NON_BURST_DID,
    burst: false,
    score: expectedRow.score.score,
    facts_used: expectedRow.score.facts_used,
    facts: expectedRow.facts,
    provenance: {
      ledger_generated_at: LEDGER.generated_at,
      log_rows: LEDGER.log_rows,
      posts: LEDGER.posts,
      dids: LEDGER.dids,
      bursts: LEDGER.bursts,
      schema: LEDGER.schema,
    },
  });
});

test("a known burst did answers 200 with the compact burst body", async () => {
  const worker = makeWorker(INDEX, LEDGER);
  const res = await worker.fetch(req(`/did/${BURST_DID}`), ALWAYS_ALLOW);
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("x-ledger-generated-at"), LEDGER.generated_at);
  const body = await res.json();
  const [burstId, firstSeenTs, postCount, maxPostsPerMinute] = LEDGER.burst[BURST_DID];
  // Pinned as a literal, not derived from `String(firstSeenTs)` -- that would make this
  // assertion tautologically self-consistent with the route's own formatting and unable to
  // catch a regression (see `openagentsearch.reputation.compact`'s cross-language parity test,
  // `test_lookup_burst_did_facts_used_matches_worker_js_number_formatting`, which pins the SAME
  // committed fixture's SAME DID on the Python side to the SAME literal -- the "one shape
  // everywhere" contract, `handoff/B2-SPEC.md` item 2). This fixture's `did:key:zqMneLidTqoZY`
  // has `first_seen_ts` 1757136000.0, a whole-second timestamp -- JS's `String()` never keeps a
  // trailing ".0" for an integral Number, where Python's native `str(float)` always does.
  assert.equal(BURST_DID, "did:key:zqMneLidTqoZY");
  assert.equal(String(firstSeenTs), "1757136000");
  assert.deepEqual(body, {
    did: BURST_DID,
    burst: true,
    burst_id: burstId,
    score: 0,
    facts_used: [
      ["burst", "true"],
      ["first_seen_ts", "1757136000"],
      ["post_count", String(postCount)],
      ["max_posts_per_minute", String(maxPostsPerMinute)],
    ],
    facts: null,
    provenance: {
      ledger_generated_at: LEDGER.generated_at,
      log_rows: LEDGER.log_rows,
      posts: LEDGER.posts,
      dids: LEDGER.dids,
      bursts: LEDGER.bursts,
      schema: LEDGER.schema,
    },
  });
});

test("an unknown well-formed did answers 404 unknown_did, with a ledger loaded", async () => {
  const worker = makeWorker(INDEX, LEDGER);
  const res = await worker.fetch(req(`/did/${UNKNOWN_DID}`), ALWAYS_ALLOW);
  assert.equal(res.status, 404);
  assert.deepEqual(await res.json(), { error: "unknown_did" });
  assert.equal(res.headers.get("x-ledger-generated-at"), LEDGER.generated_at);
});

test("a malformed did is 400 invalid_did even with a ledger loaded, header still present", async () => {
  const worker = makeWorker(INDEX, LEDGER);
  const res = await worker.fetch(req("/did/not-a-valid-did"), ALWAYS_ALLOW);
  assert.equal(res.status, 400);
  assert.deepEqual(await res.json(), { error: "invalid_did" });
  assert.equal(res.headers.get("x-ledger-generated-at"), LEDGER.generated_at);
});

test("GET /did/{did} accepts a percent-encoded DID and answers exactly like the literal one", async () => {
  const worker = makeWorker(INDEX, LEDGER);
  const literal = await worker.fetch(req(`/did/${NON_BURST_DID}`), ALWAYS_ALLOW);
  const encoded = await worker.fetch(req(`/did/${encodeURIComponent(NON_BURST_DID)}`), ALWAYS_ALLOW);
  assert.equal(encoded.status, 200);
  assert.deepEqual(await encoded.json(), await literal.json());
  const malformed = await worker.fetch(req("/did/did%3Akey%3Az%E0%A4%A"), ALWAYS_ALLOW);
  assert.equal(malformed.status, 400);
  assert.deepEqual(await malformed.json(), { error: "invalid_did" });
});

test("HEAD /did/{did} carries the same status and headers with no body", async () => {
  const worker = makeWorker(INDEX, LEDGER);
  const res = await worker.fetch(req(`/did/${NON_BURST_DID}`, { method: "HEAD" }), ALWAYS_ALLOW);
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("x-ledger-generated-at"), LEDGER.generated_at);
  assert.equal(await res.text(), "");
});

// --- GET /route (package D2) -- see worker/test/route.test.mjs for the full contract -------

test("GET /route without an offerShape loaded is 500 offer_shape_missing", async () => {
  // makeWorker(INDEX) here deliberately omits the third (offerShape) argument -- a deploy built
  // without worker/src/offer-shape.json must not silently answer a wrong shape.
  const worker = makeWorker(INDEX);
  const res = await worker.fetch(req("/route?model_hash=m1"), ALWAYS_ALLOW);
  assert.equal(res.status, 500);
  assertCommonHeaders(res);
  assert.deepEqual(await res.json(), { error: "offer_shape_missing" });
});

// --- GET /index/* redirects ----------------------------------------------------------------

const REDIRECTS = [
  ["/index/manifest.json", "https://djd39448.github.io/openagentsearch/index/manifest.json"],
  [
    "/index/flop-surface.jsonl",
    "https://djd39448.github.io/openagentsearch/index/flop-surface.jsonl",
  ],
  ["/index/lexical-v1.json", "https://djd39448.github.io/openagentsearch/index/lexical-v1.json"],
];

for (const [route, target] of REDIRECTS) {
  test(`GET ${route} redirects (302) to ${target}`, async () => {
    const worker = makeWorker(INDEX);
    const res = await worker.fetch(req(route), ALWAYS_ALLOW);
    assert.equal(res.status, 302);
    assert.equal(res.headers.get("location"), target);
    assertCommonHeaders(res);
  });
}

// --- 404, 405, 414 -------------------------------------------------------------------------

test("an unknown path is 404 not_found", async () => {
  const worker = makeWorker(INDEX);
  const res = await worker.fetch(req("/totally/unknown/path"), ALWAYS_ALLOW);
  assert.equal(res.status, 404);
  assert.deepEqual(await res.json(), { error: "not_found" });
  assertCommonHeaders(res);
});

test("a path over 256 characters is 414", async () => {
  const worker = makeWorker(INDEX);
  const res = await worker.fetch(req(`/${"a".repeat(300)}`), ALWAYS_ALLOW);
  assert.equal(res.status, 414);
  assertCommonHeaders(res);
});

// --- Rate limiter --------------------------------------------------------------------------

test("rate limiter success lets the request through", async () => {
  const worker = makeWorker(INDEX);
  const res = await worker.fetch(req("/search?q=error"), ALWAYS_ALLOW);
  assert.equal(res.status, 200);
});

test("rate limiter {success:false} is 429 with Retry-After: 60", async () => {
  const worker = makeWorker(INDEX);
  const refusing = fakeEnv(async () => ({ success: false }));
  const res = await worker.fetch(req("/search?q=error"), refusing);
  assert.equal(res.status, 429);
  assert.equal(res.headers.get("retry-after"), "60");
  assertCommonHeaders(res);
  assert.deepEqual(await res.json(), { error: "rate_limited" });
});

test("missing RATE_LIMITER binding is 503 rate_limiter_unavailable", async () => {
  const worker = makeWorker(INDEX);
  const res = await worker.fetch(req("/search?q=error"), {});
  assert.equal(res.status, 503);
  assertCommonHeaders(res);
  assert.deepEqual(await res.json(), { error: "rate_limiter_unavailable" });
});

test("RATE_LIMITER.limit() throwing is 503 rate_limiter_unavailable", async () => {
  const worker = makeWorker(INDEX);
  const throwing = fakeEnv(async () => {
    throw new Error("boom");
  });
  const res = await worker.fetch(req("/search?q=error"), throwing);
  assert.equal(res.status, 503);
  assert.deepEqual(await res.json(), { error: "rate_limiter_unavailable" });
});

test("no env at all is 503 rate_limiter_unavailable", async () => {
  const worker = makeWorker(INDEX);
  const res = await worker.fetch(req("/search?q=error"));
  assert.equal(res.status, 503);
});

test("rate limiter is applied even to an unknown path", async () => {
  const worker = makeWorker(INDEX);
  const refusing = fakeEnv(async () => ({ success: false }));
  const res = await worker.fetch(req("/nope"), refusing);
  assert.equal(res.status, 429);
});

test("rate limiter is keyed by cf-connecting-ip", async () => {
  const worker = makeWorker(INDEX);
  const seenKeys = [];
  const env = fakeEnv(async ({ key }) => {
    seenKeys.push(key);
    return { success: true };
  });
  await worker.fetch(req("/search?q=error", { headers: { "cf-connecting-ip": "203.0.113.9" } }), env);
  assert.deepEqual(seenKeys, ["203.0.113.9"]);
});
