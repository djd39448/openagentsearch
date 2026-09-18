// The GET-only JSON routes (`/`, `/healthz`, `/search`, `/did/{did}`, `/route`, `/index/*`
// redirects, 404/405/414) plus the shared rate-limiter check `worker/src/index.js` reuses for
// `/mcp`, plus (package UI1) the human-facing HTML inspector page served at `GET /` on content
// negotiation. Deliberately imports nothing beyond `./search.js` and `./page.js` and web
// standards (`Request`, `Response`, `URL`) -- no `agents`, no `@modelcontextprotocol/server`, no
// `zod`, and (package B2) no static `import ... from "../index/did-ledger-compact.json"` either,
// and (package D2) no static `import ... from "./offer-shape.json"` either -- so
// `worker/test/router.test.mjs` can exercise the whole JSON surface with plain Windows Node and
// no installed dependencies, and with no build-time dependency on the gitignored ledger file
// existing on disk. `./page.js` is itself data-free and import-free, still zero npm deps. See
// `handoff/C1-DESIGN.md` §1 for the route table this implements.
//
// Pure over its inputs: every exported function takes `index`/`ledger`/`offerShape`/`env`/
// `request` explicitly and touches no module-level mutable state. NOT guaranteed: this module does
// not itself rate-limit `/mcp` (the MCP transport lives in `index.js`, which calls
// {@link checkRateLimit} the same way this module does for its own routes) and does not know
// anything about MCP tool schemas.
//
// Package UI1 adds the human-facing `GET /` content negotiation ({@link wantsHtml}) and the HTML
// response it serves ({@link htmlResponse}), built from `./page.js`.

import { codePointLength, search } from "./search.js";
import { PAGE_HTML, PAGE_SCRIPT_SHA256, PAGE_STYLE_SHA256 } from "./page.js";

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

// `/route` (package D2) -- same alphabet for `model_hash` and `precision`, only the length bound
// differs; deliberately not validated against any fixed vocabulary (the FLOP `SessionOffer` field
// vocabulary is unpublished -- see `offer-shape.json`). Mirrors
// `openagentsearch.api.route`'s `_MODEL_HASH_RE`/`_PRECISION_RE` exactly. Exported so
// `worker/src/index.js`'s `route` MCP tool can apply the same charset check {@link parseRouteParams}
// does -- like `did_lookup` reusing {@link DID_RE}, a zod schema only bounds length/type, not
// charset.
export const MODEL_HASH_RE = /^[A-Za-z0-9:_./-]{1,128}$/;
export const PRECISION_RE = /^[A-Za-z0-9:_./-]{1,32}$/;
const MAX_LATENCY_MS = 600000;
// The `did:key:` method, multibase `z` prefix, 1-120 base58 characters -- {@link DID_RE} anchored
// to a whole string; this one is unanchored, used to find mentions inside free text (a hit's
// snippet), matching `openagentsearch.api.route`'s `_DID_FINDALL_RE`.
const DID_FINDALL_RE = /did:key:z[1-9A-HJ-NP-Za-km-z]{1,120}/g;
const ROUTE_CANDIDATES_REASON =
  "no SessionOffer shape is public; nothing in this response is an offer";
const ROUTE_RANKING_REASON =
  "no published quote unit; cross-provider ranking is fail-closed (flop-labs/yellowpaper#26)";

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

const TOOL_LIST = Object.freeze(["search", "did_lookup", "index_info", "route"]);

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
    "access-control-expose-headers":
      "x-index-generated-at, x-index-db-sha256, x-ledger-generated-at, cache-control, retry-after",
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
 * `GET /`'s content-negotiation rule (package UI1): true iff the request wants the human HTML
 * inspector page instead of the JSON service card. `?format=json` forces JSON unconditionally
 * (checked first, before any `Accept` parsing) -- there is no equivalent way to force HTML.
 * Otherwise this parses the `Accept` header per RFC 9110 (`,`-separated items, each optionally
 * carrying a `;q=` parameter, case-insensitive parameter name, trimmed whitespace) and answers
 * `true` only when `text/html` or `application/xhtml+xml` is present with a `q` STRICTLY greater
 * than both `application/json`'s `q` (when present) and the bare wildcard's `q` (when present).
 *
 * Nine consequences this implements (see `worker/test/page.test.mjs`'s Accept matrix):
 * 1. Chrome's default Accept (html, xhtml+xml, xml;q=0.9, images, wildcard;q=0.8) -> HTML.
 * 2. Firefox's default Accept (html, xhtml+xml, xml;q=0.9, wildcard;q=0.8) -> HTML.
 * 3. The bare wildcard alone (curl's default) -> JSON.
 * 4. No `Accept` header at all -> JSON.
 * 5. `application/json` alone -> JSON.
 * 6. `text/html;q=0` -> JSON (`q=0` means excluded, treated as if absent).
 * 7. `text/html, application/json` (equal, default `q=1` each) -> JSON (a tie never wins).
 * 8. `?format=json` with any `Accept`, even Chrome's -> JSON (the query param wins outright).
 * 9. `text/html;q=0.9` against the bare wildcard at `q=0.9` (a tie) -> JSON.
 * Plus: a `q` that is not a finite number in `[0, 1]` (e.g. `q=abc`) makes the WHOLE function
 * return `false` (JSON -- agents win any ambiguity); media-type matching is case-insensitive
 * (`TEXT/HTML` -> HTML); a bare wildcard subtype (`text/*`, `application/*`) is never counted as
 * either `html` or `json`, whatever its own `q`.
 *
 * @param {Request} request
 * @param {URL} url
 * @returns {boolean}
 */
export function wantsHtml(request, url) {
  if (url.searchParams.get("format") === "json") return false;
  const acceptRaw = request.headers.get("accept");
  if (!acceptRaw || acceptRaw.trim() === "") return false;

  let htmlQ = -1;
  let jsonQ = -1;
  let starQ = -1;

  for (const rawItem of acceptRaw.split(",")) {
    const parts = rawItem.split(";");
    const mediaType = parts[0].trim().toLowerCase();
    if (mediaType === "") continue;

    let q = 1;
    for (let i = 1; i < parts.length; i++) {
      const eq = parts[i].indexOf("=");
      if (eq === -1) continue;
      const paramName = parts[i].slice(0, eq).trim().toLowerCase();
      if (paramName !== "q") continue;
      const value = parts[i].slice(eq + 1).trim();
      const num = Number(value);
      if (!Number.isFinite(num) || num < 0 || num > 1) return false;
      q = num;
    }
    if (q === 0) continue; // excluded -- treated as absent, for every media type alike

    if (mediaType === "text/html" || mediaType === "application/xhtml+xml") {
      if (q > htmlQ) htmlQ = q;
    } else if (mediaType === "application/json") {
      if (q > jsonQ) jsonQ = q;
    } else if (mediaType === "*/*") {
      if (q > starQ) starQ = q;
    }
    // Any other media type, including a bare wildcard subtype like `text/*` or
    // `application/*`, is deliberately ignored -- it never counts as html or json.
  }

  if (htmlQ < 0) return false;
  if (jsonQ >= 0 && htmlQ <= jsonQ) return false;
  if (starQ >= 0 && htmlQ <= starQ) return false;
  return true;
}

/**
 * The header set for the HTML inspector page (package UI1) -- deliberately NOT
 * {@link commonHeaders}: no `access-control-allow-origin` (this is not an API response) and no
 * `permissions-policy` (dropped deliberately -- clipboard works same-origin without it). The
 * `Content-Security-Policy` pins the page's one `<style>`/`<script>` by hash, so no inline
 * handler or injected script/style can execute even if the injection rule in `./page.js` were
 * ever violated.
 *
 * @param {object} index a parsed `lexical-v1.json` document
 * @returns {Record<string, string>}
 */
function htmlHeaders(index) {
  return {
    "content-type": "text/html; charset=utf-8",
    "x-content-type-options": "nosniff",
    "x-index-generated-at": index.generated_at,
    "x-index-db-sha256": index.db_sha256,
    "cache-control": "public, max-age=300",
    vary: "Accept",
    "referrer-policy": "no-referrer",
    "content-security-policy":
      "default-src 'none'; script-src 'sha256-" +
      PAGE_SCRIPT_SHA256 +
      "'; style-src 'sha256-" +
      PAGE_STYLE_SHA256 +
      "'; connect-src 'self'; img-src 'none'; font-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
  };
}

/**
 * The `GET /` HTML response (package UI1): `PAGE_HTML` verbatim, byte-identical for every
 * request (it carries no data of its own -- see `./page.js`'s module docstring). `HEAD` answers
 * the same status and headers with no body, the same convention every JSON route uses.
 *
 * @param {object} index a parsed `lexical-v1.json` document
 * @param {string} method the original request method
 * @returns {Response}
 */
export function htmlResponse(index, method) {
  return new Response(method === "HEAD" ? null : PAGE_HTML, {
    status: 200,
    headers: htmlHeaders(index),
  });
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
 * `{dids, bursts, generated_at}` for `ledger`, or `null` when no compact ledger was loaded --
 * the same shape `openagentsearch.api.healthz._ledger_summary` produces, reused by both `GET /`
 * and `GET /healthz` (package B2).
 *
 * @param {object | null} ledger a parsed `did-ledger-compact.json` document, or `null`
 * @returns {{dids: number, bursts: number, generated_at: string} | null}
 */
function ledgerSummary(ledger) {
  if (ledger == null) return null;
  return { dids: ledger.dids, bursts: ledger.bursts, generated_at: ledger.generated_at };
}

/**
 * The `GET /` service-card body: `handoff/C1-DESIGN.md` §1's "index `generated_at`, `db_sha256`,
 * counts by kind, route + tool list, links to `docs/api.md` and the static files", plus (package
 * B2) the reputation ledger's own counts.
 *
 * NOT guaranteed: `routes`/`tools` are a fixed, hand-maintained list, not introspected from the
 * actual router or MCP server -- a route or tool added elsewhere without updating these constants
 * would not appear here.
 *
 * @param {object} index
 * @param {object | null} [ledger] a parsed `did-ledger-compact.json` document, or `null`/omitted
 * @param {string | null} [origin] the Worker's own origin (`new URL(request.url).origin`) -- when
 *   given, the card carries `inspector: "<origin>/"`, the same URL rendered for a browser (package
 *   UI1); omitted/`null` leaves the field out (a card built with no request in hand)
 * @returns {object}
 */
export function serviceCard(index, ledger = null, origin = null) {
  const card = {
    service: "openagentsearch",
    generated_at: index.generated_at,
    db_sha256: index.db_sha256,
    counts: countsByKind(index),
    routes: ROUTE_LIST,
    tools: TOOL_LIST,
    docs: "https://github.com/djd39448/openagentsearch/blob/main/docs/api.md",
    static_index: `${STATIC_INDEX_BASE}/`,
  };
  if (typeof origin === "string") card.inspector = `${origin}/`;
  card.ledger = ledgerSummary(ledger);
  return card;
}

/**
 * The `GET /healthz` body: `handoff/C1-DESIGN.md` §1's health shape, as far as a Worker holding
 * only the precomputed lexical index (never the full manifest `pipeline.publish` builds
 * `manifest.json` from) can report it, plus (package B2) the reputation ledger's own counts.
 *
 * NOT guaranteed: `index.failed` / `index.superseded` / `index.refused` are always `0` here --
 * this Worker never loads `manifest.json`, so it cannot see any document that isn't already
 * `indexed` in `lexical-v1.json`; staleness is visible only through `generated_at`. See
 * `docs/api.md`.
 *
 * @param {object} index
 * @param {object | null} [ledger] a parsed `did-ledger-compact.json` document, or `null`/omitted
 * @returns {object}
 */
export function healthzBody(index, ledger = null) {
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
    ledger: ledgerSummary(ledger),
  };
}

/**
 * The full `/did/{did}` answer for `did` (already validated against {@link DID_RE} by the
 * caller) -- `{status, body}`, the SAME shape whether reached from `GET /did/{did}` or the
 * `did_lookup` MCP tool (`handoff/B2-SPEC.md` item 2, "one shape everywhere"; the Python A2
 * server's `openagentsearch.api.did.make_did_prefix_route` mirrors this exactly).
 *
 * @param {object | null} ledger a parsed `did-ledger-compact.json` document, or `null`
 * @param {string} did already validated against {@link DID_RE}
 * @returns {{status: number, body: object}}
 */
export function lookupDid(ledger, did) {
  if (ledger == null) {
    return { status: 404, body: { error: "ledger_not_built" } };
  }
  const provenance = {
    ledger_generated_at: ledger.generated_at,
    log_rows: ledger.log_rows,
    posts: ledger.posts,
    dids: ledger.dids,
    bursts: ledger.bursts,
    schema: ledger.schema,
  };
  const row = ledger.non_burst[did];
  if (row !== undefined) {
    return {
      status: 200,
      body: {
        did,
        burst: false,
        score: row.score.score,
        facts_used: row.score.facts_used,
        facts: row.facts,
        provenance,
      },
    };
  }
  const record = ledger.burst[did];
  if (record !== undefined) {
    const [burstId, firstSeenTs, postCount, maxPostsPerMinute] = record;
    return {
      status: 200,
      body: {
        did,
        burst: true,
        burst_id: burstId,
        score: 0.0,
        facts_used: [
          ["burst", "true"],
          ["first_seen_ts", String(firstSeenTs)],
          ["post_count", String(postCount)],
          ["max_posts_per_minute", String(maxPostsPerMinute)],
        ],
        facts: null,
        provenance,
      },
    };
  }
  return { status: 404, body: { error: "unknown_did" } };
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
 * Parses and validates the `/route` query string (package D2). Returns `{error, field}` naming
 * the first problem found (`missing_model_hash`, `invalid_model_hash`, `invalid_precision`,
 * `invalid_max_latency_ms`, `invalid_k`), or `{model_hash, precision, max_latency_ms, k}` on
 * success -- mirrors `openagentsearch.api.route.parse_route_params`'s validation order and error
 * vocabulary exactly. `params.get(name)` already implements "repeated parameters: the first value
 * wins" (the same convention {@link parseSearchParams} relies on).
 *
 * NOT guaranteed: this validates shape and bounds only -- it never touches the index, so a
 * syntactically valid combination can still find zero observations.
 *
 * @param {URLSearchParams} params
 * @returns {{error: string, field: string} | {model_hash: string, precision: string | null, max_latency_ms: number | null, k: number}}
 */
export function parseRouteParams(params) {
  const modelHash = params.get("model_hash");
  if (modelHash === null) return { error: "missing_model_hash", field: "model_hash" };
  if (!MODEL_HASH_RE.test(modelHash)) {
    return { error: "invalid_model_hash", field: "model_hash" };
  }

  let precision = null;
  const precisionRaw = params.get("precision");
  if (precisionRaw !== null) {
    if (!PRECISION_RE.test(precisionRaw)) {
      return { error: "invalid_precision", field: "precision" };
    }
    precision = precisionRaw;
  }

  let maxLatencyMs = null;
  const maxLatencyRaw = params.get("max_latency_ms");
  if (maxLatencyRaw !== null) {
    if (maxLatencyRaw === "" || !K_DIGITS_RE.test(maxLatencyRaw)) {
      return { error: "invalid_max_latency_ms", field: "max_latency_ms" };
    }
    maxLatencyMs = Number.parseInt(maxLatencyRaw, 10);
    if (!Number.isInteger(maxLatencyMs) || maxLatencyMs < 1 || maxLatencyMs > MAX_LATENCY_MS) {
      return { error: "invalid_max_latency_ms", field: "max_latency_ms" };
    }
  }

  let k = DEFAULT_K;
  const kRaw = params.get("k");
  if (kRaw !== null) {
    if (kRaw === "" || !K_DIGITS_RE.test(kRaw)) return { error: "invalid_k", field: "k" };
    k = Number.parseInt(kRaw, 10);
    if (!Number.isInteger(k) || k < MIN_K || k > MAX_K) return { error: "invalid_k", field: "k" };
  }

  return { model_hash: modelHash, precision, max_latency_ms: maxLatencyMs, k };
}

/**
 * Every `did:key:z[1-9A-HJ-NP-Za-km-z]{1,120}` token found in `text`, in order of first
 * appearance, de-duplicated, cut at `limit` -- the JavaScript port of
 * `openagentsearch.api.route.extract_dids`. A mention only: this says nothing about who wrote
 * `text`, and finding none is not an error (an empty array).
 *
 * @param {string} text
 * @param {number} [limit]
 * @returns {string[]}
 */
export function extractDids(text, limit = 5) {
  const seen = new Set();
  const found = [];
  for (const match of text.matchAll(DID_FINDALL_RE)) {
    const did = match[0];
    if (seen.has(did)) continue;
    seen.add(did);
    found.push(did);
    if (found.length >= limit) break;
  }
  return found;
}

/**
 * The full `/route` body for one already-validated {@link parseRouteParams} result (package D2) --
 * shared by `GET /route` ({@link handleJsonRoute}) and the `route` MCP tool
 * (`worker/src/index.js`), the SAME "one shape everywhere" convention {@link lookupDid} uses for
 * `/did/{did}`.
 *
 * `candidates` is always `[]` and `ranking` is always `null` -- an invariant, not a fixture
 * accident, while `offerShape.published` is `false` (see `worker/src/offer-shape.json`'s own
 * generator, `scripts/make_offer_shape_json.py`, for why). `observations` are
 * `search(index, observationsQuery, k, null)`'s hits (no `kind` filter), each joined to `ledger`
 * by the `did:key:` tokens {@link extractDids} finds in the hit's `snippet`, via {@link lookupDid}
 * itself -- so a `dids[].ledger` body is byte-identical to what `/did/{did}` would answer for the
 * same DID, never re-derived.
 *
 * @param {object} index a parsed `lexical-v1.json` document
 * @param {object | null} ledger a parsed `did-ledger-compact.json` document, or `null`
 * @param {object} offerShape a parsed `offer-shape.json` document (never `null` here -- the
 *   caller checks that first)
 * @param {{model_hash: string, precision: string | null, max_latency_ms: number | null, k: number}} params
 * @returns {object}
 */
export function buildRouteBody(index, ledger, offerShape, params) {
  const { model_hash: modelHash, precision, max_latency_ms: maxLatencyMs, k } = params;
  const observationsQuery = precision === null ? modelHash : `${modelHash} ${precision}`;
  const hits = search(index, observationsQuery, k, null);
  const observations = hits.map((hit) => ({
    url: hit.doc_url,
    kind: hit.kind,
    score: hit.score,
    text: hit.snippet,
    dids: extractDids(hit.snippet).map((did) => ({ did, ledger: lookupDid(ledger, did).body })),
  }));
  return {
    query: { model_hash: modelHash, precision, max_latency_ms: maxLatencyMs, k },
    advisory: true,
    // Rebuilt field by field in the documented order (`published, source, watch, binds`) -- the
    // generated `offer-shape.json` is written with sorted keys for byte-determinism, and JSON
    // key order survives `JSON.parse`/`stringify`, so splatting the parsed file here would make
    // the Worker's bytes differ from the A2 server's for the same body.
    offer_shape: {
      published: offerShape.published,
      source: offerShape.source,
      watch: offerShape.watch,
      binds: offerShape.binds,
    },
    candidates: [],
    candidates_reason: ROUTE_CANDIDATES_REASON,
    ranking: null,
    ranking_reason: ROUTE_RANKING_REASON,
    observations,
    observations_query: observationsQuery,
    index_generated_at: index.generated_at,
    ledger_generated_at: ledger == null ? null : ledger.generated_at,
  };
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
 * @param {object | null} ledger a parsed `did-ledger-compact.json` document, or `null` when the
 *   Worker was built without one (`/did/{did}` then answers `ledger_not_built` for every
 *   well-formed DID -- package B2)
 * @param {Request} request
 * @param {unknown} env
 * @param {Set<string>} [knownKindsSet] the valid `kind` values for `index` (see {@link knownKinds});
 *   computed fresh from `index` when omitted -- callers that serve many requests over the same
 *   `index` (e.g. {@link makeWorker}) should compute it once and pass it, per the kind rule.
 * @param {object | null} [offerShape] a parsed `offer-shape.json` document, or `null`/omitted --
 *   `GET /route` then answers `500 {"error": "offer_shape_missing"}` for every request (package
 *   D2): a deploy built without this artifact must not answer a wrong shape.
 * @returns {Promise<Response>}
 */
export async function handleJsonRoute(
  index,
  ledger,
  request,
  env,
  knownKindsSet = new Set(knownKinds(index)),
  offerShape = null,
) {
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
    if (wantsHtml(request, url)) return htmlResponse(index, method);
    return jsonResponse(index, 200, serviceCard(index, ledger, url.origin), {
      method,
      cacheSeconds: CARD_CACHE_SECONDS,
      extraHeaders: { vary: "Accept" },
    });
  }

  if (pathname === "/healthz") {
    return jsonResponse(index, 200, healthzBody(index, ledger), { method, cacheSeconds: CARD_CACHE_SECONDS });
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
    // Percent-decoded like the Python A2 route (`openagentsearch.api.did`), so a client that
    // encodes the `did:key:` colons reaches the same answer a literal one does; a malformed
    // escape sequence is simply an invalid DID.
    let did = pathname.slice("/did/".length);
    try {
      did = decodeURIComponent(did);
    } catch {
      did = "";
    }
    // Present on every /did/{did} response, whatever its status, whenever a ledger is loaded --
    // there is nothing to report it as when `ledger` is `null` (package B2).
    const didHeaders = ledger == null ? {} : { "x-ledger-generated-at": ledger.generated_at };
    if (!DID_RE.test(did)) {
      return jsonResponse(index, 400, { error: "invalid_did" }, { method, extraHeaders: didHeaders });
    }
    const { status, body } = lookupDid(ledger, did);
    return jsonResponse(index, status, body, { method, extraHeaders: didHeaders });
  }

  if (pathname === "/route") {
    // Present on every /route response, whatever its status, whenever a ledger is loaded -- the
    // same convention the /did/{did} branch above uses ("present even for a 400: a ledger IS
    // loaded, so there is something to report").
    const routeLedgerHeaders =
      ledger == null ? {} : { "x-ledger-generated-at": ledger.generated_at };
    // A deploy built without `offer-shape.json` must not silently answer a wrong (stale or
    // fabricated) `offer_shape` -- fail closed instead, before any query parsing.
    if (offerShape == null) {
      return jsonResponse(index, 500, { error: "offer_shape_missing" }, {
        method,
        cacheSeconds: SEARCH_CACHE_SECONDS,
        extraHeaders: routeLedgerHeaders,
      });
    }
    const parsed = parseRouteParams(url.searchParams);
    if ("error" in parsed) {
      return jsonResponse(
        index,
        400,
        { error: parsed.error, field: parsed.field },
        { method, cacheSeconds: SEARCH_CACHE_SECONDS, extraHeaders: routeLedgerHeaders },
      );
    }
    const body = buildRouteBody(index, ledger, offerShape, parsed);
    return jsonResponse(index, 200, body, {
      method,
      cacheSeconds: SEARCH_CACHE_SECONDS,
      extraHeaders: routeLedgerHeaders,
    });
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
 * generic `404`; `worker/src/index.js`'s own `makeWorker` is what composes the two. This module
 * stays data-free (see the module docstring): `ledger` is always the caller's parsed object,
 * never imported here.
 *
 * @param {object} index a parsed `lexical-v1.json` document
 * @param {object | null} [ledger] a parsed `did-ledger-compact.json` document, or `null`/omitted
 *   (`/did/{did}` then answers `ledger_not_built` for every well-formed DID -- package B2)
 * @param {object | null} [offerShape] a parsed `offer-shape.json` document, or `null`/omitted
 *   (`GET /route` then answers `500 {"error": "offer_shape_missing"}` -- package D2)
 * @returns {{fetch: (request: Request, env?: unknown, ctx?: unknown) => Promise<Response>, handle: (request: Request, env?: unknown, ctx?: unknown) => Promise<Response>}}
 */
export function makeWorker(index, ledger = null, offerShape = null) {
  // Computed once per loaded `index`, not per request or hard-coded -- see the kind rule at
  // {@link knownKinds}.
  const knownKindsSet = new Set(knownKinds(index));
  /**
   * @param {Request} request
   * @param {unknown} [env]
   * @returns {Promise<Response>}
   */
  async function handle(request, env) {
    return handleJsonRoute(index, ledger, request, env, knownKindsSet, offerShape);
  }
  return { fetch: handle, handle };
}
