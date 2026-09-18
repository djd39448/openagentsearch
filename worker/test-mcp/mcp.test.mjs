// Package C2b, spec Tests item 4 (extended by package B2's `did_lookup`/`index_info` ledger
// cases): the composed Worker (`worker/src/index.js`, JSON routes + `/mcp`) through
// `makeWorker(fixtureIndex[, ledgerFixture])`. Needs `node_modules` (`agents`,
// `@modelcontextprotocol/server`, `zod`) so this directory runs from WSL, never plain Windows
// Node -- see the repository's test recipes. Every `Request` carries a `Host` header the MCP
// handler validates (`openagentsearch.example.workers.dev`), or it answers `403`.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import path from "node:path";

import { makeWorker } from "../src/index.js";
import { search } from "../src/search.js";
import { makeWorker as makeJsonWorker } from "../src/routes.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, "..", "..");
const INDEX = JSON.parse(
  readFileSync(
    path.join(REPO_ROOT, "tests", "fixtures", "lexical", "fixture-index-v1.json"),
    "utf-8",
  ),
);
// Package B2: the compact reputation-ledger fixture (see worker/test/router.test.mjs's own
// comment) -- used only by the `did_lookup` tests below; every other test in this file omits it
// (ledger stays `null`, so `did_lookup` keeps answering the pre-B2 `ledger_not_built` placeholder,
// unaffected by this fixture's existence).
const LEDGER = JSON.parse(
  readFileSync(
    path.join(REPO_ROOT, "tests", "fixtures", "reputation", "compact-fixture.json"),
    "utf-8",
  ),
);
// Package D2: the committed, generated offer-shape artifact the deployed Worker bundles -- used by
// the `route` tests below; the other tests omit it (so `route` answers `offer_shape_missing`).
const OFFER_SHAPE = JSON.parse(
  readFileSync(path.join(REPO_ROOT, "worker", "src", "offer-shape.json"), "utf-8"),
);
const NON_BURST_DID = Object.keys(LEDGER.non_burst).sort()[0];
const BURST_DID = Object.keys(LEDGER.burst)[0];
const UNKNOWN_DID = "did:key:z6MkfVWRHNeiV99ckgHDmi8HpwMLtir1XsTu9rNCoYdTuizf";
// Package LM3: the LM1-committed liveness-compact fixture, the same one
// `worker/test/liveness.test.mjs` (plain Node) loads -- used only by the `liveness` tests below;
// every other test in this file omits it (liveness stays `null`).
const LIVENESS = JSON.parse(
  readFileSync(
    path.join(REPO_ROOT, "tests", "fixtures", "liveness", "liveness-compact.expected.json"),
    "utf-8",
  ),
);
const KNOWN_ROOM = "contrib-like";
const KNOWN_LIVENESS_DID = Object.keys(LIVENESS.agents).sort()[0];

const HOST = "openagentsearch.example.workers.dev";
const BASE = `https://${HOST}`;

function fakeEnv(limitImpl = async () => ({ success: true })) {
  return { RATE_LIMITER: { limit: limitImpl } };
}

const ALWAYS_ALLOW = fakeEnv();

let nextId = 100;

/**
 * Parses a Streamable-HTTP response body -- plain JSON (`Content-Type: application/json`) or a
 * one-shot SSE stream (`event: message\ndata: <json>\n\n`, this Worker's legacy-compatibility
 * lane's default shape for a stateless single request/response exchange) -- and returns the
 * JSON-RPC message it carries. Throws if neither shape is present.
 *
 * @param {Response} res
 * @returns {Promise<any>}
 */
async function readRpcMessage(res) {
  const contentType = res.headers.get("content-type") || "";
  const text = await res.text();
  if (contentType.includes("application/json")) {
    return JSON.parse(text);
  }
  if (contentType.includes("text/event-stream")) {
    for (const block of text.split("\n\n")) {
      const dataLine = block.split("\n").find((line) => line.startsWith("data: "));
      if (dataLine) return JSON.parse(dataLine.slice("data: ".length));
    }
    throw new Error(`no SSE data frame found in body: ${text}`);
  }
  throw new Error(`unexpected content-type ${contentType}: ${text}`);
}

/**
 * @param {object} body a JSON-RPC request/notification object (jsonrpc/id/method/params)
 * @param {{env?: unknown, headers?: Record<string, string>, method?: string, ledger?: object | null}} [opts]
 * @returns {Promise<Response>}
 */
async function postMcp(body, opts = {}) {
  const {
    env = ALWAYS_ALLOW,
    headers = {},
    method = "POST",
    ledger = null,
    offerShape = null,
    liveness = null,
  } = opts;
  const worker = makeWorker(INDEX, ledger, offerShape, liveness);
  const request = new Request(`${BASE}/mcp`, {
    method,
    headers: {
      "content-type": "application/json",
      accept: "application/json, text/event-stream",
      host: HOST,
      ...headers,
    },
    body: method === "GET" || method === "HEAD" ? undefined : JSON.stringify(body),
  });
  return worker.fetch(request, env);
}

function rpc(method, params) {
  return { jsonrpc: "2.0", id: nextId++, method, params };
}

// --- initialize ------------------------------------------------------------------------------

test("initialize echoes the protocol version and reports serverInfo.name", async () => {
  const res = await postMcp(
    rpc("initialize", {
      protocolVersion: "2025-06-18",
      capabilities: {},
      clientInfo: { name: "mcp-test-client", version: "0.0.0" },
    }),
  );
  assert.equal(res.status, 200);
  const message = await readRpcMessage(res);
  assert.equal(message.result.protocolVersion, "2025-06-18");
  assert.equal(message.result.serverInfo.name, "openagentsearch");
});

// --- tools/list --------------------------------------------------------------------------

test("tools/list lists exactly search, did_lookup, index_info, route, liveness with the documented schemas", async () => {
  const res = await postMcp(rpc("tools/list", {}));
  const message = await readRpcMessage(res);
  const tools = message.result.tools;
  assert.deepEqual(
    tools.map((t) => t.name).sort(),
    ["did_lookup", "index_info", "liveness", "route", "search"],
  );

  const byName = Object.fromEntries(tools.map((t) => [t.name, t]));

  assert.deepEqual(byName.search.inputSchema.required, ["q"]);
  assert.deepEqual(Object.keys(byName.search.inputSchema.properties).sort(), ["k", "kind", "q"]);
  // `kind` is a plain bounded string, not a fixed enum -- it is validated against the kinds
  // actually present in the loaded index inside the handler (see the tests below), per
  // `handoff/C2b-SPEC.md` §3's kind rule ("never hard-code the list").
  assert.equal(byName.search.inputSchema.properties.kind.type, "string");
  assert.equal(byName.search.inputSchema.properties.kind.enum, undefined);
  assert.equal(byName.search.inputSchema.properties.kind.minLength, 1);
  assert.equal(byName.search.inputSchema.properties.kind.maxLength, 32);
  assert.equal(byName.search.inputSchema.properties.k.default, 10);

  assert.deepEqual(byName.did_lookup.inputSchema.required, ["did"]);
  // `did` is a plain bounded string in the SCHEMA, not `z.string().regex(...)` -- like `kind`
  // above, the `did:key` shape is checked inside the handler instead, so a malformed value
  // reaches the app-level `{"error": "invalid_did"}` body (`handoff/B2-SPEC.md` item 4) rather
  // than being short-circuited into the SDK's own free-text schema-validation error.
  assert.equal(byName.did_lookup.inputSchema.properties.did.type, "string");
  assert.equal(byName.did_lookup.inputSchema.properties.did.pattern, undefined);
  assert.equal(byName.did_lookup.inputSchema.properties.did.minLength, 1);
  assert.equal(byName.did_lookup.inputSchema.properties.did.maxLength, 200);

  assert.deepEqual(Object.keys(byName.index_info.inputSchema.properties || {}), []);

  assert.deepEqual(Object.keys(byName.liveness.inputSchema.properties).sort(), ["did", "room"]);
  assert.equal(byName.liveness.inputSchema.required, undefined); // both optional
  assert.equal(byName.liveness.inputSchema.properties.room.type, "string");
  assert.equal(byName.liveness.inputSchema.properties.room.pattern, undefined);
  assert.equal(byName.liveness.inputSchema.properties.room.minLength, 1);
  assert.equal(byName.liveness.inputSchema.properties.room.maxLength, 128);
  assert.equal(byName.liveness.inputSchema.properties.did.type, "string");
  assert.equal(byName.liveness.inputSchema.properties.did.minLength, 1);
  assert.equal(byName.liveness.inputSchema.properties.did.maxLength, 200);
});

// --- tools/call search ---------------------------------------------------------------------

test("tools/call search matches the ranking test's expectation for the same query", async () => {
  const res = await postMcp(
    rpc("tools/call", { name: "search", arguments: { q: "authentication", k: 5 } }),
  );
  const message = await readRpcMessage(res);
  assert.equal(message.result.isError, undefined);
  const body = JSON.parse(message.result.content[0].text);
  const expected = search(INDEX, "authentication", 5, null);
  assert.deepEqual(body.results, expected);
  assert.equal(body.query, "authentication");
  assert.equal(body.k, 5);
});

test("tools/call search honors the kind filter", async () => {
  const res = await postMcp(
    rpc("tools/call", { name: "search", arguments: { q: "integration", kind: "site" } }),
  );
  const message = await readRpcMessage(res);
  const body = JSON.parse(message.result.content[0].text);
  assert.ok(body.results.every((r) => r.kind === "site"));
});

test("tools/call search with an unknown kind is isError:true with the same invalid_kind body GET /search uses", async () => {
  const res = await postMcp(
    rpc("tools/call", { name: "search", arguments: { q: "integration", kind: "not_a_real_kind" } }),
  );
  const message = await readRpcMessage(res);
  assert.equal(message.result.isError, true);
  const body = JSON.parse(message.result.content[0].text);
  assert.deepEqual(body, {
    error: "invalid_kind",
    known: ["github_doc", "github_issue", "room", "site"],
  });
});

// --- tools/call did_lookup -----------------------------------------------------------------

test("tools/call did_lookup answers isError:true with ledger_not_built for a well-formed did", async () => {
  const res = await postMcp(
    rpc("tools/call", {
      name: "did_lookup",
      arguments: { did: UNKNOWN_DID },
    }),
  );
  const message = await readRpcMessage(res);
  assert.equal(message.result.isError, true);
  const body = JSON.parse(message.result.content[0].text);
  assert.deepEqual(body, { error: "ledger_not_built" });
});

test("tools/call did_lookup answers isError:false for a known non-burst did, with a ledger loaded", async () => {
  const res = await postMcp(
    rpc("tools/call", { name: "did_lookup", arguments: { did: NON_BURST_DID } }),
    { ledger: LEDGER },
  );
  const message = await readRpcMessage(res);
  assert.equal(message.result.isError, false);
  const body = JSON.parse(message.result.content[0].text);
  assert.equal(body.did, NON_BURST_DID);
  assert.equal(body.burst, false);
  assert.equal(body.score, LEDGER.non_burst[NON_BURST_DID].score.score);
  assert.ok(body.facts);
});

test("tools/call did_lookup answers isError:false for a known burst did (score 0, facts null)", async () => {
  const res = await postMcp(
    rpc("tools/call", { name: "did_lookup", arguments: { did: BURST_DID } }),
    { ledger: LEDGER },
  );
  const message = await readRpcMessage(res);
  // A burst member is still a successful 200 lookup -- only invalid_did/unknown_did/
  // ledger_not_built are isError:true (handoff/B2-SPEC.md item 4).
  assert.equal(message.result.isError, false);
  const body = JSON.parse(message.result.content[0].text);
  assert.equal(body.burst, true);
  assert.equal(body.score, 0);
  assert.equal(body.facts, null);
});

test("tools/call did_lookup answers isError:true with unknown_did for an unknown well-formed did", async () => {
  const res = await postMcp(
    rpc("tools/call", { name: "did_lookup", arguments: { did: UNKNOWN_DID } }),
    { ledger: LEDGER },
  );
  const message = await readRpcMessage(res);
  assert.equal(message.result.isError, true);
  const body = JSON.parse(message.result.content[0].text);
  assert.deepEqual(body, { error: "unknown_did" });
});

test("tools/call did_lookup matches GET /did/{did} exactly (route parity)", async () => {
  for (const did of [NON_BURST_DID, BURST_DID, UNKNOWN_DID, "not-a-did"]) {
    const mcpRes = await postMcp(
      rpc("tools/call", { name: "did_lookup", arguments: { did } }),
      { ledger: LEDGER },
    );
    const message = await readRpcMessage(mcpRes);
    const mcpBody = JSON.parse(message.result.content[0].text);

    const jsonWorker = makeJsonWorker(INDEX, LEDGER);
    const routeRes = await jsonWorker.fetch(
      new Request(`${BASE}/did/${did}`, { method: "GET" }),
      ALWAYS_ALLOW,
    );
    const routeBody = await routeRes.json();

    assert.deepEqual(mcpBody, routeBody, did);
  }
});

// --- tools/call route (package D2) --------------------------------------------------------------

test("tools/call route matches GET /route exactly (route parity), with and without a ledger", async () => {
  const cases = [
    { args: { model_hash: "hello", precision: "fp16", max_latency_ms: 1500, k: 3 }, ledger: LEDGER },
    { args: { model_hash: "hello" }, ledger: LEDGER },
    { args: { model_hash: "hello", k: 2 }, ledger: null },
  ];
  for (const { args, ledger } of cases) {
    const mcpRes = await postMcp(rpc("tools/call", { name: "route", arguments: args }), {
      ledger,
      offerShape: OFFER_SHAPE,
    });
    const message = await readRpcMessage(mcpRes);
    assert.notEqual(message.result.isError, true, JSON.stringify(args));
    const mcpBody = JSON.parse(message.result.content[0].text);

    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(args)) params.set(key, String(value));
    const jsonWorker = makeJsonWorker(INDEX, ledger, OFFER_SHAPE);
    const routeRes = await jsonWorker.fetch(
      new Request(`${BASE}/route?${params.toString()}`, { method: "GET" }),
      ALWAYS_ALLOW,
    );
    assert.equal(routeRes.status, 200);
    const routeBody = await routeRes.json();

    assert.deepEqual(mcpBody, routeBody, JSON.stringify(args));
    assert.deepEqual(mcpBody.candidates, []);
    assert.equal(mcpBody.ranking, null);
    assert.deepEqual(mcpBody.offer_shape, OFFER_SHAPE);
  }
});

test("tools/call route answers the app-level offer_shape_missing body when no offer shape is loaded", async () => {
  const res = await postMcp(
    rpc("tools/call", { name: "route", arguments: { model_hash: "hello" } }),
    { ledger: LEDGER },
  );
  const message = await readRpcMessage(res);
  assert.equal(message.result.isError, true);
  assert.deepEqual(JSON.parse(message.result.content[0].text), { error: "offer_shape_missing" });
});

test("tools/call route rejects a model_hash outside the alphabet with the app-level body, not a schema error", async () => {
  const res = await postMcp(
    rpc("tools/call", { name: "route", arguments: { model_hash: "has space" } }),
    { ledger: LEDGER, offerShape: OFFER_SHAPE },
  );
  const message = await readRpcMessage(res);
  assert.equal(message.result.isError, true);
  assert.deepEqual(JSON.parse(message.result.content[0].text), {
    error: "invalid_model_hash",
    field: "model_hash",
  });
});

// --- tools/call liveness (package LM3) -----------------------------------------------------

test("tools/call liveness with no arguments matches GET /liveness exactly (route parity)", async () => {
  const mcpRes = await postMcp(rpc("tools/call", { name: "liveness", arguments: {} }), {
    liveness: LIVENESS,
  });
  const message = await readRpcMessage(mcpRes);
  assert.notEqual(message.result.isError, true);
  const mcpBody = JSON.parse(message.result.content[0].text);

  const jsonWorker = makeJsonWorker(INDEX, null, null, LIVENESS);
  const routeRes = await jsonWorker.fetch(new Request(`${BASE}/liveness`, { method: "GET" }), ALWAYS_ALLOW);
  assert.equal(routeRes.status, 200);
  const routeBody = await routeRes.json();

  assert.deepEqual(mcpBody, routeBody);
  assert.equal(mcpBody.agents, undefined);
});

test("tools/call liveness with room matches GET /liveness/room/{room} exactly (route parity)", async () => {
  const mcpRes = await postMcp(
    rpc("tools/call", { name: "liveness", arguments: { room: KNOWN_ROOM } }),
    { liveness: LIVENESS },
  );
  const message = await readRpcMessage(mcpRes);
  assert.equal(message.result.isError, false);
  const mcpBody = JSON.parse(message.result.content[0].text);

  const jsonWorker = makeJsonWorker(INDEX, null, null, LIVENESS);
  const routeRes = await jsonWorker.fetch(
    new Request(`${BASE}/liveness/room/${KNOWN_ROOM}`, { method: "GET" }),
    ALWAYS_ALLOW,
  );
  assert.equal(routeRes.status, 200);
  assert.deepEqual(mcpBody, await routeRes.json());
});

test("tools/call liveness with did matches GET /liveness/agent/{did} exactly (route parity)", async () => {
  const mcpRes = await postMcp(
    rpc("tools/call", { name: "liveness", arguments: { did: KNOWN_LIVENESS_DID } }),
    { liveness: LIVENESS },
  );
  const message = await readRpcMessage(mcpRes);
  assert.equal(message.result.isError, false);
  const mcpBody = JSON.parse(message.result.content[0].text);

  const jsonWorker = makeJsonWorker(INDEX, null, null, LIVENESS);
  const routeRes = await jsonWorker.fetch(
    new Request(`${BASE}/liveness/agent/${encodeURIComponent(KNOWN_LIVENESS_DID)}`, { method: "GET" }),
    ALWAYS_ALLOW,
  );
  assert.equal(routeRes.status, 200);
  assert.deepEqual(mcpBody, await routeRes.json());
});

test("tools/call liveness with both room and did is isError:true one_of_room_or_did, checked before any lookup", async () => {
  const res = await postMcp(
    rpc("tools/call", { name: "liveness", arguments: { room: "not-a-real-room", did: "not-a-did" } }),
    { liveness: LIVENESS },
  );
  const message = await readRpcMessage(res);
  assert.equal(message.result.isError, true);
  assert.deepEqual(JSON.parse(message.result.content[0].text), { error: "one_of_room_or_did" });
});

test("tools/call liveness rejects a malformed room/did with the app-level body, not a schema error", async () => {
  const roomRes = await postMcp(
    rpc("tools/call", { name: "liveness", arguments: { room: "has space" } }),
    { liveness: LIVENESS },
  );
  const roomMessage = await readRpcMessage(roomRes);
  assert.equal(roomMessage.result.isError, true);
  assert.deepEqual(JSON.parse(roomMessage.result.content[0].text), { error: "invalid_room" });

  const didRes = await postMcp(
    rpc("tools/call", { name: "liveness", arguments: { did: "not-a-valid-did" } }),
    { liveness: LIVENESS },
  );
  const didMessage = await readRpcMessage(didRes);
  assert.equal(didMessage.result.isError, true);
  assert.deepEqual(JSON.parse(didMessage.result.content[0].text), { error: "invalid_did" });
});

test("tools/call liveness answers the app-level unknown_room/unknown_agent bodies", async () => {
  const roomRes = await postMcp(
    rpc("tools/call", { name: "liveness", arguments: { room: "never-polled-room" } }),
    { liveness: LIVENESS },
  );
  const roomMessage = await readRpcMessage(roomRes);
  assert.equal(roomMessage.result.isError, true);
  assert.deepEqual(JSON.parse(roomMessage.result.content[0].text), { error: "unknown_room" });

  const didRes = await postMcp(
    rpc("tools/call", { name: "liveness", arguments: { did: UNKNOWN_DID } }),
    { liveness: LIVENESS },
  );
  const didMessage = await readRpcMessage(didRes);
  assert.equal(didMessage.result.isError, true);
  assert.deepEqual(JSON.parse(didMessage.result.content[0].text), { error: "unknown_agent" });
});

test("tools/call liveness answers isError:true liveness_not_built when built without the map (no args / room / did)", async () => {
  for (const args of [{}, { room: KNOWN_ROOM }, { did: KNOWN_LIVENESS_DID }]) {
    const res = await postMcp(rpc("tools/call", { name: "liveness", arguments: args }));
    const message = await readRpcMessage(res);
    assert.equal(message.result.isError, true, JSON.stringify(args));
    assert.deepEqual(
      JSON.parse(message.result.content[0].text),
      { error: "liveness_not_built" },
      JSON.stringify(args),
    );
  }
});

// --- tools/call index_info ------------------------------------------------------------------

test("tools/call index_info includes the ledger counts when a ledger is loaded", async () => {
  const res = await postMcp(rpc("tools/call", { name: "index_info", arguments: {} }), {
    ledger: LEDGER,
  });
  const message = await readRpcMessage(res);
  const body = JSON.parse(message.result.content[0].text);
  assert.deepEqual(body.ledger, {
    dids: LEDGER.dids,
    bursts: LEDGER.bursts,
    generated_at: LEDGER.generated_at,
  });
});

// --- invalid arguments -----------------------------------------------------------------------

test("invalid tool arguments produce the SDK's own validation error", async () => {
  const res = await postMcp(rpc("tools/call", { name: "search", arguments: { q: "" } }));
  const message = await readRpcMessage(res);
  // The SDK reports schema-validation failures as an isError tool result (not a top-level
  // JSON-RPC error), distinct from the "unknown tool" case below.
  assert.equal(message.result.isError, true);
  assert.match(message.result.content[0].text, /[Vv]alidation/);
});

test("a did that does not match the did:key pattern answers the app-level invalid_did body, not a schema error", async () => {
  const res = await postMcp(
    rpc("tools/call", { name: "did_lookup", arguments: { did: "not-a-did" } }),
  );
  const message = await readRpcMessage(res);
  assert.equal(message.result.isError, true);
  const body = JSON.parse(message.result.content[0].text);
  assert.deepEqual(body, { error: "invalid_did" });
});

test("an empty did is still rejected by the schema (bounds, not shape)", async () => {
  const res = await postMcp(rpc("tools/call", { name: "did_lookup", arguments: { did: "" } }));
  const message = await readRpcMessage(res);
  assert.equal(message.result.isError, true);
});

// --- unknown tool ----------------------------------------------------------------------------

test("an unknown tool name is JSON-RPC error -32602", async () => {
  const res = await postMcp(rpc("tools/call", { name: "does_not_exist", arguments: {} }));
  const message = await readRpcMessage(res);
  assert.equal(message.error.code, -32602);
});

// --- transport-level behavior --------------------------------------------------------------

test("GET /mcp is 405", async () => {
  const res = await postMcp(undefined, { method: "GET" });
  assert.equal(res.status, 405);
});

test("a request with no Host header is 403", async () => {
  const worker = makeWorker(INDEX);
  const request = new Request(`${BASE}/mcp`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      accept: "application/json, text/event-stream",
    },
    body: JSON.stringify(rpc("tools/list", {})),
  });
  const res = await worker.fetch(request, ALWAYS_ALLOW);
  assert.equal(res.status, 403);
});

// --- rate limiting (runs before the MCP handler) --------------------------------------------

test("rate limiter {success:false} is 429 before the MCP handler runs", async () => {
  let handlerCalled = false;
  const worker = makeWorker(INDEX);
  const request = new Request(`${BASE}/mcp`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      accept: "application/json, text/event-stream",
      host: HOST,
    },
    body: JSON.stringify(rpc("tools/list", {})),
  });
  const env = fakeEnv(async () => {
    handlerCalled = true; // marks that the limiter itself ran; the MCP transport never touches it
    return { success: false };
  });
  const res = await worker.fetch(request, env);
  assert.equal(res.status, 429);
  assert.equal(res.headers.get("retry-after"), "60");
  assert.deepEqual(await res.json(), { error: "rate_limited" });
  assert.ok(handlerCalled);
});

test("missing RATE_LIMITER binding is 503 rate_limiter_unavailable", async () => {
  const res = await postMcp(rpc("tools/list", {}), { env: {} });
  assert.equal(res.status, 503);
  assert.deepEqual(await res.json(), { error: "rate_limiter_unavailable" });
});
