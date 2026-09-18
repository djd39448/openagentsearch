// Package LM3, spec Tests item: `GET /liveness`, `GET /liveness/room/{room}`,
// `GET /liveness/agent/{did}` (`worker/src/routes.js`) -- against `makeWorker(fixtureIndex[,
// ledgerFixture, offerShape, livenessFixture])`, deliberately NOT `worker/src/index.js` (see
// `worker/test/router.test.mjs`'s own comment for why: this stays plain-Node, dependency-free).
//
// Uses the LM1-committed fixture map `tests/fixtures/liveness/liveness-compact.expected.json`
// (`-text` in `.gitattributes`) -- the same artifact `tests/test_liveness_build.py` regenerates
// and pins on the Python side, so a room/agent shape check here is checked against genuine
// counted facts, not a hand-rolled stand-in.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import path from "node:path";

import { makeWorker } from "../src/routes.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, "..", "..");

const INDEX = JSON.parse(
  readFileSync(path.join(REPO_ROOT, "tests", "fixtures", "lexical", "fixture-index-v1.json"), "utf-8"),
);
const LEDGER = JSON.parse(
  readFileSync(path.join(REPO_ROOT, "tests", "fixtures", "reputation", "compact-fixture.json"), "utf-8"),
);
const OFFER_SHAPE = JSON.parse(readFileSync(path.join(HERE, "..", "src", "offer-shape.json"), "utf-8"));
const LIVENESS = JSON.parse(
  readFileSync(
    path.join(REPO_ROOT, "tests", "fixtures", "liveness", "liveness-compact.expected.json"),
    "utf-8",
  ),
);

const KNOWN_ROOM = "contrib-like";
const KNOWN_DID = Object.keys(LIVENESS.agents).sort()[0];
// Well-formed room id / DID, absent from the fixture map.
const UNKNOWN_ROOM = "never-polled-room";
const UNKNOWN_DID = "did:key:z6MkfVWRHNeiV99ckgHDmi8HpwMLtir1XsTu9rNCoYdTuizf";

const BASE = "https://openagentsearch.example.workers.dev";

function req(pathAndQuery, { method = "GET", headers } = {}) {
  return new Request(`${BASE}${pathAndQuery}`, { method, headers });
}

function fakeEnv(limitImpl) {
  return { RATE_LIMITER: { limit: limitImpl } };
}

const ALWAYS_ALLOW = fakeEnv(async () => ({ success: true }));

function makeLiveWorker() {
  return makeWorker(INDEX, LEDGER, OFFER_SHAPE, LIVENESS);
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
}

// --- GET /liveness -----------------------------------------------------------------------

test("GET /liveness answers the compact map minus agents, field for field, in the documented order", async () => {
  const worker = makeLiveWorker();
  const res = await worker.fetch(req("/liveness"), ALWAYS_ALLOW);
  assert.equal(res.status, 200);
  assertCommonHeaders(res);
  assert.equal(res.headers.get("cache-control"), "public, max-age=300");
  assert.equal(res.headers.get("x-liveness-generated-at"), LIVENESS.generated_at);
  const body = await res.json();
  assert.deepEqual(Object.keys(body), [
    "schema",
    "generated_at",
    "window_days",
    "log_rows",
    "ledger_generated_at",
    "counts",
    "method",
    "rooms",
  ]);
  assert.deepEqual(body, {
    schema: LIVENESS.schema,
    generated_at: LIVENESS.generated_at,
    window_days: LIVENESS.window_days,
    log_rows: LIVENESS.log_rows,
    ledger_generated_at: LIVENESS.ledger_generated_at,
    counts: LIVENESS.counts,
    method: LIVENESS.method,
    rooms: LIVENESS.rooms,
  });
  assert.equal(body.agents, undefined);
});

test("HEAD /liveness returns the same status and headers with no body", async () => {
  const worker = makeLiveWorker();
  const res = await worker.fetch(req("/liveness", { method: "HEAD" }), ALWAYS_ALLOW);
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("x-liveness-generated-at"), LIVENESS.generated_at);
  const text = await res.text();
  assert.equal(text, "");
});

test("POST /liveness is 405 with Allow: GET, HEAD", async () => {
  const worker = makeLiveWorker();
  const res = await worker.fetch(req("/liveness", { method: "POST" }), ALWAYS_ALLOW);
  assert.equal(res.status, 405);
  assert.equal(res.headers.get("allow"), "GET, HEAD");
});

test("GET /liveness is rate-limited (429) when the binding refuses", async () => {
  const worker = makeLiveWorker();
  const refusing = fakeEnv(async () => ({ success: false }));
  const res = await worker.fetch(req("/liveness"), refusing);
  assert.equal(res.status, 429);
  assert.equal(res.headers.get("retry-after"), "60");
  assert.deepEqual(await res.json(), { error: "rate_limited" });
});

// --- GET /liveness/room/{room} -------------------------------------------------------------

test("a known room answers 200 with class/class_all/signals/decided_on/facts/provenance", async () => {
  const worker = makeLiveWorker();
  const res = await worker.fetch(req(`/liveness/room/${KNOWN_ROOM}`), ALWAYS_ALLOW);
  assert.equal(res.status, 200);
  assertCommonHeaders(res);
  assert.equal(res.headers.get("x-liveness-generated-at"), LIVENESS.generated_at);
  const body = await res.json();
  const fixtureRow = LIVENESS.rooms[KNOWN_ROOM];
  assert.deepEqual(body, {
    room: KNOWN_ROOM,
    class: fixtureRow.class,
    class_all: fixtureRow.class_all,
    signals: fixtureRow.signals,
    decided_on: fixtureRow.decided_on,
    facts: fixtureRow.facts,
    provenance: {
      liveness_generated_at: LIVENESS.generated_at,
      window_days: LIVENESS.window_days,
      log_rows: LIVENESS.log_rows,
      ledger_generated_at: LIVENESS.ledger_generated_at,
      schema: LIVENESS.schema,
    },
  });
});

test("HEAD /liveness/room/{room} returns the same status and headers with no body", async () => {
  const worker = makeLiveWorker();
  const res = await worker.fetch(req(`/liveness/room/${KNOWN_ROOM}`, { method: "HEAD" }), ALWAYS_ALLOW);
  assert.equal(res.status, 200);
  const text = await res.text();
  assert.equal(text, "");
});

test("a well-formed but absent room answers 404 unknown_room", async () => {
  const worker = makeLiveWorker();
  const res = await worker.fetch(req(`/liveness/room/${UNKNOWN_ROOM}`), ALWAYS_ALLOW);
  assert.equal(res.status, 404);
  assert.deepEqual(await res.json(), { error: "unknown_room" });
  assert.equal(res.headers.get("x-liveness-generated-at"), LIVENESS.generated_at);
});

for (const bad of ["", "has space", "has/slash", "a".repeat(129)]) {
  test(`an invalid room id (${JSON.stringify(bad)}) answers 400 invalid_room`, async () => {
    const worker = makeLiveWorker();
    const res = await worker.fetch(req(`/liveness/room/${encodeURIComponent(bad)}`), ALWAYS_ALLOW);
    assert.equal(res.status, 400);
    assert.deepEqual(await res.json(), { error: "invalid_room" });
  });
}

test("a room id at exactly 128 characters is accepted (well-formed, just absent)", async () => {
  const worker = makeLiveWorker();
  const res = await worker.fetch(req(`/liveness/room/${"a".repeat(128)}`), ALWAYS_ALLOW);
  assert.equal(res.status, 404);
  assert.deepEqual(await res.json(), { error: "unknown_room" });
});

// --- GET /liveness/agent/{did} -------------------------------------------------------------

test("a known agent answers 200 with every signal mapped by position, did_note_present a boolean", async () => {
  const worker = makeLiveWorker();
  const res = await worker.fetch(req(`/liveness/agent/${KNOWN_DID}`), ALWAYS_ALLOW);
  assert.equal(res.status, 200);
  assertCommonHeaders(res);
  assert.equal(res.headers.get("x-liveness-generated-at"), LIVENESS.generated_at);
  const body = await res.json();
  const [
    tier,
    points,
    roomsCount,
    liveRoomsCount,
    replyIn,
    replyInNonburst,
    replyOut,
    workCycles,
    templateRows,
    faucetOnboardingRows,
    githubContribRows,
    didNotePresent,
    postCount,
    unsignedRows,
    distinctTextRatio,
    ageDays,
  ] = LIVENESS.agents[KNOWN_DID];
  assert.equal(LIVENESS.agents[KNOWN_DID].length, 16);
  assert.deepEqual(body, {
    did: KNOWN_DID,
    tier,
    points,
    signals: {
      post_count: postCount,
      unsigned_rows: unsignedRows,
      rooms_count: roomsCount,
      live_rooms_count: liveRoomsCount,
      reply_in: replyIn,
      reply_in_nonburst: replyInNonburst,
      reply_out: replyOut,
      work_cycles: workCycles,
      template_rows: templateRows,
      faucet_onboarding_rows: faucetOnboardingRows,
      github_contrib_rows: githubContribRows,
      did_note_present: Boolean(didNotePresent),
      distinct_text_ratio: distinctTextRatio,
      age_days: ageDays,
    },
    thresholds: {
      agent_points: LIVENESS.method.agent_points,
      tier_thresholds: LIVENESS.method.tier_thresholds,
      agent_min_posts: LIVENESS.method.constants.AGENT_MIN_POSTS,
    },
    provenance: {
      liveness_generated_at: LIVENESS.generated_at,
      window_days: LIVENESS.window_days,
      log_rows: LIVENESS.log_rows,
      ledger_generated_at: LIVENESS.ledger_generated_at,
      schema: LIVENESS.schema,
    },
  });
  assert.equal(typeof body.signals.did_note_present, "boolean");
});

test("a well-formed but absent DID answers 404 unknown_agent", async () => {
  const worker = makeLiveWorker();
  const res = await worker.fetch(req(`/liveness/agent/${UNKNOWN_DID}`), ALWAYS_ALLOW);
  assert.equal(res.status, 404);
  assert.deepEqual(await res.json(), { error: "unknown_agent" });
  assert.equal(res.headers.get("x-liveness-generated-at"), LIVENESS.generated_at);
});

test("a malformed did answers 400 invalid_did, the same code /did uses", async () => {
  const worker = makeLiveWorker();
  const res = await worker.fetch(req("/liveness/agent/not-a-valid-did"), ALWAYS_ALLOW);
  assert.equal(res.status, 400);
  assert.deepEqual(await res.json(), { error: "invalid_did" });
});

test("GET /liveness/agent/{did} accepts a percent-encoded DID and answers exactly like the literal one", async () => {
  const worker = makeLiveWorker();
  const literal = await worker.fetch(req(`/liveness/agent/${KNOWN_DID}`), ALWAYS_ALLOW);
  const encoded = await worker.fetch(req(`/liveness/agent/${encodeURIComponent(KNOWN_DID)}`), ALWAYS_ALLOW);
  assert.equal(encoded.status, 200);
  assert.deepEqual(await encoded.json(), await literal.json());
});

test("POST /liveness/agent/{did} is 405", async () => {
  const worker = makeLiveWorker();
  const res = await worker.fetch(req(`/liveness/agent/${KNOWN_DID}`, { method: "POST" }), ALWAYS_ALLOW);
  assert.equal(res.status, 405);
});

// --- liveness_not_built (fail closed) ------------------------------------------------------

test("makeWorker(index, ledger, offerShape, null): every /liveness* route answers 404 liveness_not_built", async () => {
  const worker = makeWorker(INDEX, LEDGER, OFFER_SHAPE, null);
  for (const p of ["/liveness", `/liveness/room/${KNOWN_ROOM}`, `/liveness/agent/${KNOWN_DID}`]) {
    const res = await worker.fetch(req(p), ALWAYS_ALLOW);
    assert.equal(res.status, 404, p);
    assert.deepEqual(await res.json(), { error: "liveness_not_built" }, p);
    assert.equal(res.headers.get("x-liveness-generated-at"), null, p);
  }
});

test("makeWorker(index): omitting liveness (and ledger/offerShape) defaults to the same liveness_not_built answer", async () => {
  const worker = makeWorker(INDEX);
  for (const p of ["/liveness", `/liveness/room/${KNOWN_ROOM}`, `/liveness/agent/${KNOWN_DID}`]) {
    const res = await worker.fetch(req(p), ALWAYS_ALLOW);
    assert.equal(res.status, 404, p);
    assert.deepEqual(await res.json(), { error: "liveness_not_built" }, p);
  }
});

test("an invalid room/did still answers its own 400 even with no liveness map loaded", async () => {
  const worker = makeWorker(INDEX);
  const roomRes = await worker.fetch(req("/liveness/room/has space"), ALWAYS_ALLOW);
  assert.equal(roomRes.status, 400);
  assert.deepEqual(await roomRes.json(), { error: "invalid_room" });
  const didRes = await worker.fetch(req("/liveness/agent/not-a-valid-did"), ALWAYS_ALLOW);
  assert.equal(didRes.status, 400);
  assert.deepEqual(await didRes.json(), { error: "invalid_did" });
});

// --- card / healthz liveness block -----------------------------------------------------------

test("GET / carries a liveness block after ledger, with rooms/agents/generated_at", async () => {
  const worker = makeLiveWorker();
  const res = await worker.fetch(req("/"), ALWAYS_ALLOW);
  const body = await res.json();
  const keys = Object.keys(body);
  assert.equal(keys.indexOf("liveness"), keys.indexOf("ledger") + 1);
  assert.deepEqual(body.liveness, {
    rooms: Object.keys(LIVENESS.rooms).length,
    agents: Object.keys(LIVENESS.agents).length,
    generated_at: LIVENESS.generated_at,
  });
  assert.deepEqual(body.routes.slice(5, 8), [
    "GET /liveness",
    "GET /liveness/room/{room}",
    "GET /liveness/agent/{did}",
  ]);
  assert.equal(body.tools[body.tools.length - 1], "liveness");
});

test("GET / liveness is null when no map is loaded", async () => {
  const worker = makeWorker(INDEX, LEDGER, OFFER_SHAPE);
  const res = await worker.fetch(req("/"), ALWAYS_ALLOW);
  const body = await res.json();
  assert.equal(body.liveness, null);
});

test("GET /healthz carries a liveness block with rooms/agents/rooms_by_class/agents_by_tier/generated_at", async () => {
  const worker = makeLiveWorker();
  const res = await worker.fetch(req("/healthz"), ALWAYS_ALLOW);
  const body = await res.json();
  assert.deepEqual(body.liveness, {
    rooms: Object.keys(LIVENESS.rooms).length,
    agents: Object.keys(LIVENESS.agents).length,
    rooms_by_class: LIVENESS.counts.rooms_by_class,
    agents_by_tier: LIVENESS.counts.agents_by_tier,
    generated_at: LIVENESS.generated_at,
  });
});

test("GET /healthz liveness is null when no map is loaded", async () => {
  const worker = makeWorker(INDEX, LEDGER, OFFER_SHAPE);
  const res = await worker.fetch(req("/healthz"), ALWAYS_ALLOW);
  const body = await res.json();
  assert.equal(body.liveness, null);
});
