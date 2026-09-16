// The GET-only JSON routes (`/`, `/healthz`, `/search`, `/did/{did}`, `/route`, `/index/*`
// redirects, 404/405/414) plus the shared rate-limiter check `worker/src/index.js` reuses for
// `/mcp`. Deliberately imports nothing beyond `./search.js` and web standards (`Request`,
// `Response`, `URL`) -- no `agents`, no `@modelcontextprotocol/server`, no `zod` -- so
// `worker/test/router.test.mjs` can exercise the whole JSON surface with plain Windows Node and
// no installed dependencies. See `handoff/C1-DESIGN.md` §1 for the route table this implements.
//
// Pure over its inputs: every exported function takes `index`/`env`/`request` explicitly and
// touches no module-level mutable state. NOT guaranteed: this module does not itself rate-limit
// `/mcp` (the MCP transport lives in `index.js`, which calls {@link checkRateLimit} the same way
// this module does for its own routes) and does not know anything about MCP tool schemas.

import { codePointLength, search } from "./search.js";

/**
 * The set of valid `kind` values for `index` -- **never hard-coded** (`handoff/C2b-SPEC.md` §3's
 * kind rule): every distinct `doc.kind` actually present in `index.docs`, sorted by code point.
 * `openagentsearch.sources.base._KIND_RE` allows any `^[a-z_]+$` string, so this set changes as
 * new source kinds are indexed (e.g. `html` for crawled flop.finance pages, later `room_message`)
 * -- callers must compute it fresh for each loaded `index`, not cache it across index reloads.
 * See `docs/api.md`.
 *
 * @param {object} index a parsed `lexical-v1.json` document
 * @returns {string[]}
 */
export function knownKinds(index) {
  return [...new Set(index.docs.map((doc) => doc.kind))].sort();
}

/** `did:key:` method, multibase `z` (base58btc) prefix, 1-120 base58 characters after it --
 * matches `handoff/C1-DESIGN.md` §1's documented `/did/{did}` format. */
export const DID_RE = /^did:key:z[1-9A-HJ-NP-Za-km-z]{1,120}$/;

/** Base URL the `/index/*` routes redirect to -- the GitHub Pages copies documented in
 * `docs/static-index.md`. */
export const STATIC_INDEX_BASE = "https://djd39448.github.io/openagentsearch";

const MAX_PATH_LENGTH = 256;
const MAX_QUERY_LENGTH = 512;
const DEFAULT_K = 10;
const MIN_K = 1;
const MAX_K = 50;
const SEARCH_CACHE_SECONDS = 60;
const CARD_CACHE_SECONDS = 300;
const K_DIGITS_RE = /^[0-9]+$/;

const INDEX_REDIRECTS = new Map([
  ["/index/manifest.json", `${STATIC_INDEX_BASE}/index/manifest.json`],
  ["/index/flop-surface.jsonl", `${STATIC_INDEX_BASE}/index/flop-surface.jsonl`],
  ["/index/lexical-v1.json", `${STATIC_INDEX_BASE}/index/lexical-v1.json`],
]);

const ROUTE_LIST = Object.freeze([
  "GET /",
  "GET /healthz",
  "GET /search",
  "GET /did/{did}",
  "GET /route",
  "GET /index/manifest.json",
  "GET /index/flop-surface.jsonl",
  "GET /index/lexical-v1.json",
  "POST /mcp",
]);

const TOOL_LIST = Object.freeze(["search", "did_lookup", "index_info"]);

/**
 * The common header set every response carries, `Cache-Control` aside (passed by the caller,
 * since it differs between `/search` and everything else) -- see `docs/api.md`.
 *
 * @param {object} index a parsed `lexical-v1.json` document
 * @param {number} cacheSeconds
 * @param {Record<string, string>} [extra]
 * @returns {Record<string, string>}
 */
function commonHeaders(index, cacheSeconds, extra = {}) {
  return {
    "content-type": "application/json; charset=utf-8",
    "access-control-allow-origin": "*",
    "x-content-type-options": "nosniff",
    "x-index-generated-at": index.generated_at,
    "x-index-db-sha256": index.db_sha256,
    "cache-control": `public, max-age=${cacheSeconds}`,
    ...extra,
  };
}

/**
 * Builds a JSON response with the common header set applied. `method === "HEAD"` produces the
 * same status and headers with no body, per the documented `HEAD` contract for every GET route.
 *
 * NOT guaranteed: this does not itself decide `status` or `cacheSeconds` for any particular
 * route -- callers own that; this function only shapes the Response consistently once they have.
 *
 * @param {object} index
 * @param {number} status
 * @param {object} body
 * @param {{method?: string, cacheSeconds?: number, extraHeaders?: Record<string, string>}} [opts]
 * @returns {Response}
 */
export function jsonResponse(index, status, body, opts = {}) {
  const { method = "GET", cacheSeconds = CARD_CACHE_SECONDS, extraHeaders = {} } = opts;
  const headers = commonHeaders(index, cacheSeconds, extraHeaders);
  if (method === "HEAD") return new Response(null, { status, headers });
  return new Response(`${JSON.stringify(body)}\n`, { status, headers });
}

function redirectResponse(index, target) {
  const headers = commonHeaders(index, CARD_CACHE_SECONDS, { location: target });
  return new Response(null, { status: 302, headers });
}

/**
 * The GA Workers Rate Limiting check every route except `/` and `/healthz` runs (including
 * `/mcp`, applied by `worker/src/index.js` before invoking the MCP transport): keyed by
 * `cf-connecting-ip`, fails CLOSED (`503 rate_limiter_unavailable`) when `env.RATE_LIMITER` is
 * missing or its `.limit()` call throws, and answers `429` with `Retry-After: 60` when the
 * binding itself reports `{success: false}`.
 *
 * NOT guaranteed: this never retries a thrown `.limit()` call, and does not itself decide which
 * routes are exempt -- callers (this module's own `handleJsonRoute`, and `worker/src/index.js`
 * for `/mcp`) are responsible for skipping the call on `/` and `/healthz`.
 *
 * @param {object} index
 * @param {unknown} env
 * @param {Request} request
 * @param {string} method the original request method (`HEAD` responses carry no body)
 * @returns {Promise<Response | null>} a Response to return immediately, or `null` to continue
 */
export async function checkRateLimit(index, env, request, method = "GET") {
  const limiter = env && typeof env === "object" ? /** @type {any} */ (env).RATE_LIMITER : undefined;
  if (!limiter || typeof limiter.limit !== "function") {
    return jsonResponse(index, 503, { error: "rate_limiter_unavailable" }, { method });
  }
  const key = request.headers.get("cf-connecting-ip") || "unknown";
  let result;
  try {
    result = await limiter.limit({ key });
  } catch {
    return jsonResponse(index, 503, { error: "rate_limiter_unavailable" }, { method });
  }
  if (!result || result.success !== true) {
    return jsonResponse(index, 429, { error: "rate_limited" }, {
      method,
      extraHeaders: { "retry-after": "60" },
    });
  }
  return null;
}

/**
 * `{kind: count}` over `index.docs`, sorted by kind (the same code-point order {@link knownKinds}
 * returns).
 *
 * @param {object} index
 * @returns {Record<string, number>}
 */
function countsByKind(index) {
  /** @type {Record<string, number>} */
  const counts = {};
  for (const doc of index.docs) {
    counts[doc.kind] = (counts[doc.kind] || 0) + 1;
  }
  /** @type {Record<string, number>} */
  const sorted = {};
  for (const kind of Object.keys(counts).sort()) sorted[kind] = counts[kind];
  return sorted;
}

/**
 * The `GET /` service-card body: `handoff/C1-DESIGN.md` §1's "index `generated_at`, `db_sha256`,
 * counts by kind, route + tool list, links to `docs/api.md` and the static files".
 *
 * NOT guaranteed: `routes`/`tools` are a fixed, hand-maintained list, not introspected from the
 * actual router or MCP server -- a route or tool added elsewhere without updating these constants
 * would not appear here.
 *
 * @param {object} index
 * @returns {object}
 */
export function serviceCard(index) {
  return {
    service: "openagentsearch",
    generated_at: index.generated_at,
    db_sha256: index.db_sha256,
    counts: countsByKind(index),
    routes: ROUTE_LIST,
    tools: TOOL_LIST,
    docs: "https://github.com/djd39448/openagentsearch/blob/main/docs/api.md",
    static_index: `${STATIC_INDEX_BASE}/`,
  };
}

/**
 * The `GET /healthz` body: `handoff/C1-DESIGN.md` §1's health shape, as far as a Worker holding
 * only the precomputed lexical index (never the full manifest `pipeline.publish` builds
 * `manifest.json` from) can report it.
 *
 * NOT guaranteed: `index.failed` / `index.superseded` / `index.refused` are always `0` here --
 * this Worker never loads `manifest.json`, so it cannot see any document that isn't already
 * `indexed` in `lexical-v1.json`; staleness is visible only through `generated_at`. See
 * `docs/api.md`.
 *
 * @param {object} index
 * @returns {object}
 */
export function healthzBody(index) {
  return {
    status: "ok",
    index: {
      indexed: index.counts.docs,
      failed: 0,
      superseded: 0,
      refused: 0,
    },
    kinds: countsByKind(index),
    generated_at: index.generated_at,
    db_sha256: index.db_sha256,
    lexical: {
      docs: index.counts.docs,
      terms: index.counts.terms,
      postings: index.counts.postings,
    },
  };
}

/**
 * Parses and validates the `/search` query string. Returns `{error}` naming the first problem
 * found (`missing_query`, `query_too_long`, `invalid_k`, `invalid_kind`), or `{q, k, kind}` on
 * success -- mirrors `openagentsearch.api.search.make_search_route`'s validation order and error
 * vocabulary, extended with the `kind` check A2 does not have.
 *
 * NOT guaranteed: this validates shape and bounds only -- it never touches the index for `q`/`k`,
 * so a syntactically valid `q`/`k`/`kind` combination can still return zero results. `kind` IS
 * checked against the index, via `knownKindsSet` (see {@link knownKinds}) -- the caller must
 * compute that set from the same `index` the eventual `search()` call will use.
 *
 * @param {URLSearchParams} params
 * @param {Set<string>} knownKindsSet the valid `kind` values for the loaded index
 * @returns {{error: string} | {q: string, k: number, kind: string | null}}
 */
export function parseSearchParams(params, knownKindsSet) {
  const q = params.get("q");
  if (q === null || q.trim() === "") return { error: "missing_query" };
  if (codePointLength(q) > MAX_QUERY_LENGTH) return { error: "query_too_long" };

  let k = DEFAULT_K;
  const kRaw = params.get("k");
  if (kRaw !== null) {
    if (kRaw === "" || !K_DIGITS_RE.test(kRaw)) return { error: "invalid_k" };
    k = Number.parseInt(kRaw, 10);
    if (!Number.isInteger(k) || k < MIN_K || k > MAX_K) return { error: "invalid_k" };
  }

  let kind = null;
  const kindRaw = params.get("kind");
  if (kindRaw !== null) {
    if (!knownKindsSet.has(kindRaw)) return { error: "invalid_kind" };
    kind = kindRaw;
  }

  return { q, k, kind };
}

/**
 * Handles every JSON route (everything except `/mcp`, which `worker/src/index.js` owns) for one
 * request: path-length bound, the rate limiter (all routes except `/` and `/healthz`), method
 * validation (`405` + `Allow` for anything but `GET`/`HEAD`), and the route table itself. Always
 * returns a `Response` -- this function never throws for a well-formed `Request`.
 *
 * NOT guaranteed: `request.body` is never read here (every route this function serves is
 * GET/HEAD-shaped); a malformed `request.url` propagates as whatever `new URL()` itself throws,
 * not as a caught 4xx.
 *
 * @param {object} index a parsed `lexical-v1.json` document
 * @param {Request} request
 * @param {unknown} env
 * @param {Set<string>} [knownKindsSet] the valid `kind` values for `index` (see {@link knownKinds});
 *   computed fresh from `index` when omitted -- callers that serve many requests over the same
 *   `index` (e.g. {@link makeWorker}) should compute it once and pass it, per the kind rule.
 * @returns {Promise<Response>}
 */
export async function handleJsonRoute(index, request, env, knownKindsSet = new Set(knownKinds(index))) {
  const { method } = request;
  const url = new URL(request.url);
  const { pathname } = url;

  if (pathname.length > MAX_PATH_LENGTH) {
    return jsonResponse(index, 414, { error: "path_too_long" }, { method });
  }

  const isDid = pathname.startsWith("/did/");
  const isIndexRedirect = INDEX_REDIRECTS.has(pathname);
  const matched =
    pathname === "/" ||
    pathname === "/healthz" ||
    pathname === "/search" ||
    pathname === "/route" ||
    isDid ||
    isIndexRedirect;

  const rateLimitExempt = pathname === "/" || pathname === "/healthz";
  if (!rateLimitExempt) {
    const limited = await checkRateLimit(index, env, request, method);
    if (limited) return limited;
  }

  if (matched && method !== "GET" && method !== "HEAD") {
    return jsonResponse(index, 405, { error: "method_not_allowed" }, {
      method,
      extraHeaders: { allow: "GET, HEAD" },
    });
  }

  if (pathname === "/") {
    return jsonResponse(index, 200, serviceCard(index), { method, cacheSeconds: CARD_CACHE_SECONDS });
  }

  if (pathname === "/healthz") {
    return jsonResponse(index, 200, healthzBody(index), { method, cacheSeconds: CARD_CACHE_SECONDS });
  }

  if (pathname === "/search") {
    const parsed = parseSearchParams(url.searchParams, knownKindsSet);
    if ("error" in parsed) {
      const body =
        parsed.error === "invalid_kind"
          ? { error: parsed.error, known: [...knownKindsSet] }
          : { error: parsed.error };
      return jsonResponse(index, 400, body, { method, cacheSeconds: SEARCH_CACHE_SECONDS });
    }
    const { q, k, kind } = parsed;
    const results = search(index, q, k, kind);
    return jsonResponse(index, 200, { query: q, k, results }, { method, cacheSeconds: SEARCH_CACHE_SECONDS });
  }

  if (isDid) {
    const did = pathname.slice("/did/".length);
    if (!DID_RE.test(did)) {
      return jsonResponse(index, 400, { error: "invalid_did" }, { method });
    }
    // Until package B2 builds the reputation ledger, every well-formed DID answers the same way.
    return jsonResponse(index, 404, { error: "ledger_not_built" }, { method });
  }

  if (pathname === "/route") {
    // Reserved for package D2.
    return jsonResponse(index, 404, { error: "not_found" }, { method });
  }

  if (isIndexRedirect) {
    return redirectResponse(index, /** @type {string} */ (INDEX_REDIRECTS.get(pathname)));
  }

  return jsonResponse(index, 404, { error: "not_found" }, { method });
}

/**
 * Builds a JSON-routes-only Worker over `index` -- everything `handoff/C1-DESIGN.md` §1 documents
 * except `/mcp` itself (composed on top of this in `worker/src/index.js`). This is the shape
 * `worker/test/router.test.mjs` drives directly so the JSON surface is testable without `agents`
 * or the MCP SDK installed.
 *
 * NOT guaranteed: this worker has no `/mcp` route at all -- a request for it falls through to the
 * generic `404`; `worker/src/index.js`'s own `makeWorker` is what composes the two.
 *
 * @param {object} index a parsed `lexical-v1.json` document
 * @returns {{fetch: (request: Request, env?: unknown, ctx?: unknown) => Promise<Response>, handle: (request: Request, env?: unknown, ctx?: unknown) => Promise<Response>}}
 */
export function makeWorker(index) {
  // Computed once per loaded `index`, not per request or hard-coded -- see the kind rule at
  // {@link knownKinds}.
  const knownKindsSet = new Set(knownKinds(index));
  /**
   * @param {Request} request
   * @param {unknown} [env]
   * @returns {Promise<Response>}
   */
  async function handle(request, env) {
    return handleJsonRoute(index, request, env, knownKindsSet);
  }
  return { fetch: handle, handle };
}
