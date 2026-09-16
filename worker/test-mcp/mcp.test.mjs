// Package C2b, spec Tests item 4: the composed Worker (`worker/src/index.js`, JSON routes +
// `/mcp`) through `makeWorker(fixtureIndex)`. Needs `node_modules` (`agents`,
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

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, "..", "..");
const INDEX = JSON.parse(
  readFileSync(
    path.join(REPO_ROOT, "tests", "fixtures", "lexical", "fixture-index-v1.json"),
    "utf-8",
  ),
);

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
 * @param {{env?: unknown, headers?: Record<string, string>, method?: string}} [opts]
 * @returns {Promise<Response>}
 */
async function postMcp(body, opts = {}) {
  const { env = ALWAYS_ALLOW, headers = {}, method = "POST" } = opts;
  const worker = makeWorker(INDEX);
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

test("tools/list lists exactly search, did_lookup, index_info with the documented schemas", async () => {
  const res = await postMcp(rpc("tools/list", {}));
  const message = await readRpcMessage(res);
  const tools = message.result.tools;
  assert.deepEqual(
    tools.map((t) => t.name).sort(),
    ["did_lookup", "index_info", "search"],
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
  assert.equal(
    byName.did_lookup.inputSchema.properties.did.pattern,
    "^did:key:z[1-9A-HJ-NP-Za-km-z]{1,120}$",
  );

  assert.deepEqual(Object.keys(byName.index_info.inputSchema.properties || {}), []);
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
      arguments: { did: "did:key:z6MkfVWRHNeiV99ckgHDmi8HpwMLtir1XsTu9rNCoYdTuizf" },
    }),
  );
  const message = await readRpcMessage(res);
  assert.equal(message.result.isError, true);
  const body = JSON.parse(message.result.content[0].text);
  assert.deepEqual(body, { error: "ledger_not_built" });
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

test("a did that does not match the did:key pattern is rejected by the schema", async () => {
  const res = await postMcp(
    rpc("tools/call", { name: "did_lookup", arguments: { did: "not-a-did" } }),
  );
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
