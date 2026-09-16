// OpenAgentSearch public endpoint: GET-only JSON routes (`./routes.js`) plus a remote MCP server
// at `/mcp` (Streamable HTTP, stateless -- no Durable Object, no session state) exposing `search`,
// `did_lookup` and `index_info` as tools, built on `agents/mcp/server`'s `createMcpHandler` and
// the MCP SDK v2 `McpServer`. See `handoff/C1-DESIGN.md` §1 for the full route/tool table and
// `handoff/C2b-SPEC.md` for this package's deliverable.
//
// This is the ONLY module in `worker/src/` that imports `agents` or `@modelcontextprotocol/*` --
// `./routes.js` and `./search.js` stay dependency-free so `worker/test/` runs under plain Node
// with no installed packages. `worker/test-mcp/` (which imports this module) needs `node_modules`
// and runs from WSL; see the repository's test recipes.
//
// NOT guaranteed: no rate limiting beyond `env.RATE_LIMITER` (fails closed to 503 when the
// binding is missing or throws -- see `./routes.js`'s `checkRateLimit`); no per-request globals;
// no `fetch()` anywhere in this module or the ones it composes; no `eval`.

import { McpServer } from "@modelcontextprotocol/server";
import { createMcpHandler } from "agents/mcp/server";
import { z } from "zod";

import INDEX from "../index/lexical-v1.json" with { type: "json" };
import PACKAGE from "../package.json" with { type: "json" };
import { search } from "./search.js";
import { DID_RE, checkRateLimit, handleJsonRoute, healthzBody, knownKinds } from "./routes.js";

const SERVICE_NAME = "openagentsearch";

/**
 * Builds one stateless MCP server over `index`: pure tool handlers with no per-request globals,
 * matching `handoff/C2b-SPEC.md` §3's three tools exactly. A fresh server is built per request by
 * `createMcpHandler` (never reused across requests), so nothing here may hold state between
 * calls.
 *
 * NOT guaranteed: this does not itself apply the rate limiter or the `Host`/`Origin` checks --
 * those happen around it, in `makeWorker` (this module) and inside `createMcpHandler` itself.
 *
 * @param {object} index a parsed `lexical-v1.json` document
 * @returns {McpServer}
 */
export function createServer(index) {
  const server = new McpServer({ name: SERVICE_NAME, version: PACKAGE.version });
  // Computed from `index`, not hard-coded -- see the kind rule at `./routes.js`'s `knownKinds`.
  // `kind` itself stays a plain bounded string in the schema (not `z.enum`) so an unknown value
  // is reported by the handler as the documented `invalid_kind`/`known` body, the same shape
  // `GET /search` uses, rather than colliding with the SDK's own schema-validation error.
  const knownKindsList = knownKinds(index);
  const knownKindsSet = new Set(knownKindsList);

  server.registerTool(
    "search",
    {
      description:
        "BM25 lexical search over the OpenAgentSearch index (lexical, not semantic -- keyword " +
        "overlap only). Returns the same body as GET /search.",
      inputSchema: z.object({
        q: z.string().min(1).max(512),
        k: z.number().int().min(1).max(50).default(10),
        kind: z.string().min(1).max(32).optional(),
      }),
    },
    async ({ q, k, kind }) => {
      if (kind !== undefined && !knownKindsSet.has(kind)) {
        return {
          content: [
            { type: "text", text: JSON.stringify({ error: "invalid_kind", known: knownKindsList }) },
          ],
          isError: true,
        };
      }
      const results = search(index, q, k, kind ?? null);
      const body = { query: q, k, results };
      return { content: [{ type: "text", text: JSON.stringify(body) }] };
    },
  );

  server.registerTool(
    "did_lookup",
    {
      description:
        "Look up a did:key identity on the OpenAgentSearch reputation ledger. The ledger is not " +
        "built yet (package B2); every well-formed did:key currently answers ledger_not_built.",
      inputSchema: z.object({
        did: z.string().regex(DID_RE),
      }),
    },
    // eslint-disable-next-line no-unused-vars
    async ({ did }) => ({
      content: [{ type: "text", text: JSON.stringify({ error: "ledger_not_built" }) }],
      isError: true,
    }),
  );

  server.registerTool(
    "index_info",
    {
      description: "Index freshness and counts -- the same body as GET /healthz.",
      inputSchema: z.object({}),
    },
    async () => ({
      content: [{ type: "text", text: JSON.stringify(healthzBody(index)) }],
    }),
  );

  return server;
}

/**
 * Composes the JSON router (`./routes.js`) with the `/mcp` MCP transport into one Worker-shaped
 * `{fetch, handle}`. The rate limiter runs before the MCP handler, keyed and rated the same way
 * as every other non-exempt route (see `./routes.js`'s `checkRateLimit`).
 *
 * NOT guaranteed: `/mcp`'s `Host`/`Origin` validation and Streamable HTTP framing are entirely
 * `createMcpHandler`'s; this function only decides whether to reach it at all.
 *
 * @param {object} index a parsed `lexical-v1.json` document
 * @returns {{fetch: (request: Request, env?: unknown, ctx?: unknown) => Promise<Response>, handle: (request: Request, env?: unknown, ctx?: unknown) => Promise<Response>}}
 */
export function makeWorker(index) {
  const mcpHandler = createMcpHandler(() => createServer(index), { route: "/mcp" });
  // Computed once per loaded `index`, not per request or hard-coded -- see the kind rule at
  // `./routes.js`'s `knownKinds`. (`createServer` above computes its own copy per MCP request,
  // since `createMcpHandler` builds a fresh server per request; this one backs only the JSON
  // `/search` route reached via `handleJsonRoute`.)
  const knownKindsSet = new Set(knownKinds(index));

  /**
   * @param {Request} request
   * @param {unknown} [env]
   * @param {unknown} [ctx]
   * @returns {Promise<Response>}
   */
  async function handle(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname === "/mcp") {
      const limited = await checkRateLimit(index, env, request, request.method);
      if (limited) return limited;
      return mcpHandler(request, env, ctx);
    }
    return handleJsonRoute(index, request, env, knownKindsSet);
  }

  return { fetch: handle, handle };
}

const worker = makeWorker(INDEX);

/** The default-index-bound handler, exported standalone for callers that want it directly. */
export const handle = worker.handle;

export default worker;
