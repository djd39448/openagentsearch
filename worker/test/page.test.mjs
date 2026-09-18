// Package UI1, spec Tests item D: the `GET /` content-negotiation surface (`wantsHtml`,
// `htmlResponse`, `worker/src/routes.js`) and the inspector page itself (`worker/src/page.js`) --
// the Accept matrix, the JSON agent contract staying byte-identical (pinned against the captured
// live card `worker/test/fixtures/service-card.before.json`), the CSP hash pin, page hygiene over
// `PAGE_HTML`/`PAGE_SCRIPT`, that every other route ignores `Accept: text/html`, that
// `Access-Control-Expose-Headers` is on every JSON response and absent from the HTML one, and a
// direct unit test of `wantsHtml` itself. Plain Node (`node:test`, `node:assert/strict`,
// `node:crypto`, `node:fs`, `node:path`, `node:url`) against `makeWorker` from `../src/routes.js`
// -- deliberately NOT `../src/index.js` (see `worker/test/router.test.mjs`'s own comment for why).

import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import path from "node:path";

import { makeWorker, wantsHtml } from "../src/routes.js";
import { PAGE_HTML, PAGE_SCRIPT, PAGE_SCRIPT_SHA256, PAGE_STYLE, PAGE_STYLE_SHA256 } from "../src/page.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, "..", "..");

// Same fixtures `worker/test/router.test.mjs` loads.
const INDEX = JSON.parse(
  readFileSync(path.join(REPO_ROOT, "tests", "fixtures", "lexical", "fixture-index-v1.json"), "utf-8"),
);
const LEDGER = JSON.parse(
  readFileSync(path.join(REPO_ROOT, "tests", "fixtures", "reputation", "compact-fixture.json"), "utf-8"),
);
// Same `offer-shape.json` load `worker/test/route.test.mjs` uses.
const OFFER_SHAPE = JSON.parse(readFileSync(path.join(HERE, "..", "src", "offer-shape.json"), "utf-8"));

const NON_BURST_DID = Object.keys(LEDGER.non_burst).sort()[0];

const FIXTURE_CARD_PATH = path.join(HERE, "fixtures", "service-card.before.json");
const FIXTURE_CARD_BYTES = readFileSync(FIXTURE_CARD_PATH);
const FIXTURE_CARD_TEXT = FIXTURE_CARD_BYTES.toString("utf-8");
const FIXTURE_CARD = JSON.parse(FIXTURE_CARD_TEXT);

const BASE = "https://openagentsearch.example.workers.dev";

function req(pathAndQuery, opts = {}) {
  const { method = "GET", headers } = opts;
  return new Request(`${BASE}${pathAndQuery}`, { method, headers });
}

function fakeEnv(limitImpl) {
  return { RATE_LIMITER: { limit: limitImpl } };
}

const ALWAYS_ALLOW = fakeEnv(async () => ({ success: true }));

const CHROME_ACCEPT =
  "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8";
const FIREFOX_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8";

const ACCEPT_CASES = [
  { name: "chrome default accept -> html", accept: CHROME_ACCEPT, expect: "html" },
  { name: "firefox default accept -> html", accept: FIREFOX_ACCEPT, expect: "html" },
  { name: "bare wildcard (curl) -> json", accept: "*/*", expect: "json" },
  { name: "no accept header -> json", accept: undefined, expect: "json" },
  { name: "application/json -> json", accept: "application/json", expect: "json" },
  { name: "text/html;q=0 -> json", accept: "text/html;q=0", expect: "json" },
  { name: "text/html, application/json (tie) -> json", accept: "text/html, application/json", expect: "json" },
  { name: "?format=json wins over chrome accept -> json", accept: CHROME_ACCEPT, format: "json", expect: "json" },
  { name: "text/html;q=0.9 vs wildcard;q=0.9 (tie) -> json", accept: "text/html;q=0.9, */*;q=0.9", expect: "json" },
  { name: "text/html;q=abc -> json (invalid q)", accept: "text/html;q=abc", expect: "json" },
  { name: "TEXT/HTML (case-insensitive) -> html", accept: "TEXT/HTML", expect: "html" },
  { name: "text/* (bare wildcard subtype) -> json", accept: "text/*", expect: "json" },
];

// --- 1. Accept matrix ------------------------------------------------------------------------

for (const c of ACCEPT_CASES) {
  test(`GET / Accept matrix: ${c.name}`, { timeout: 10000 }, async () => {
    const worker = makeWorker(INDEX, LEDGER, OFFER_SHAPE);
    const qs = c.format ? `?format=${c.format}` : "";
    const headers = c.accept === undefined ? undefined : { accept: c.accept };
    const res = await worker.fetch(req(`/${qs}`, { headers }), ALWAYS_ALLOW);
    assert.equal(res.status, 200);
    if (c.expect === "html") {
      assert.equal(res.headers.get("content-type"), "text/html; charset=utf-8");
      assert.equal(res.headers.get("vary"), "Accept");
      const body = await res.text();
      assert.ok(body.startsWith("<!doctype html>"));
      assert.equal(body, PAGE_HTML);
    } else {
      assert.equal(res.headers.get("content-type"), "application/json; charset=utf-8");
      assert.equal(res.headers.get("vary"), "Accept");
      const body = await res.text();
      assert.doesNotThrow(() => JSON.parse(body));
    }
  });
}

// --- 2. Agent contract unchanged --------------------------------------------------------------

test("GET / JSON body: key order matches the captured live fixture", { timeout: 10000 }, async () => {
  const worker = makeWorker(INDEX, LEDGER, OFFER_SHAPE);
  const res = await worker.fetch(req("/"), ALWAYS_ALLOW);
  const text = await res.text();
  const body = JSON.parse(text);
  assert.deepEqual(Object.keys(body), Object.keys(FIXTURE_CARD));
});

test("GET / JSON body: service/routes/tools/docs/static_index match the fixture's", { timeout: 10000 }, async () => {
  const worker = makeWorker(INDEX, LEDGER, OFFER_SHAPE);
  const res = await worker.fetch(req("/"), ALWAYS_ALLOW);
  const body = await res.json();
  assert.equal(body.service, FIXTURE_CARD.service);
  assert.deepEqual(body.routes, FIXTURE_CARD.routes);
  assert.deepEqual(body.tools, FIXTURE_CARD.tools);
  assert.equal(body.docs, FIXTURE_CARD.docs);
  assert.equal(body.static_index, FIXTURE_CARD.static_index);
});

test("GET / JSON body: generated_at/db_sha256/ledger/counts reflect the loaded index+ledger", { timeout: 10000 }, async () => {
  const worker = makeWorker(INDEX, LEDGER, OFFER_SHAPE);
  const res = await worker.fetch(req("/"), ALWAYS_ALLOW);
  const body = await res.json();
  assert.equal(body.generated_at, INDEX.generated_at);
  assert.equal(body.db_sha256, INDEX.db_sha256);
  assert.deepEqual(body.ledger, {
    dids: LEDGER.dids,
    bursts: LEDGER.bursts,
    generated_at: LEDGER.generated_at,
  });
  const expectedCounts = {};
  for (const doc of INDEX.docs) expectedCounts[doc.kind] = (expectedCounts[doc.kind] || 0) + 1;
  const sortedExpected = {};
  for (const kind of Object.keys(expectedCounts).sort()) sortedExpected[kind] = expectedCounts[kind];
  assert.deepEqual(body.counts, sortedExpected);
});

test("GET / JSON body text is byte-identical across the JSON-forcing Accept variants", { timeout: 10000 }, async () => {
  const worker = makeWorker(INDEX, LEDGER, OFFER_SHAPE);
  const [starRes, noneRes, formatRes] = await Promise.all([
    worker.fetch(req("/", { headers: { accept: "*/*" } }), ALWAYS_ALLOW),
    worker.fetch(req("/"), ALWAYS_ALLOW),
    worker.fetch(req("/?format=json", { headers: { accept: CHROME_ACCEPT } }), ALWAYS_ALLOW),
  ]);
  const [starText, noneText, formatText] = await Promise.all([starRes.text(), noneRes.text(), formatRes.text()]);
  assert.equal(starText, noneText);
  assert.equal(noneText, formatText);
});

test("the fixture file itself: ends with }\\n and JSON.parse preserves its key order", { timeout: 10000 }, () => {
  assert.equal(FIXTURE_CARD_BYTES[FIXTURE_CARD_BYTES.length - 2], 0x7d); // '}'
  assert.equal(FIXTURE_CARD_BYTES[FIXTURE_CARD_BYTES.length - 1], 0x0a); // '\n'
  const reparsed = JSON.parse(FIXTURE_CARD_TEXT);
  assert.deepEqual(Object.keys(reparsed), Object.keys(FIXTURE_CARD));
});

// --- 3. HTML headers -------------------------------------------------------------------------

test("HTML headers: full header set on GET /", { timeout: 10000 }, async () => {
  const worker = makeWorker(INDEX, LEDGER, OFFER_SHAPE);
  const res = await worker.fetch(req("/", { headers: { accept: CHROME_ACCEPT } }), ALWAYS_ALLOW);
  assert.equal(res.status, 200);
  const expectedCsp =
    "default-src 'none'; script-src 'sha256-" +
    PAGE_SCRIPT_SHA256 +
    "'; style-src 'sha256-" +
    PAGE_STYLE_SHA256 +
    "'; connect-src 'self'; img-src 'none'; font-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'";
  assert.equal(res.headers.get("content-security-policy"), expectedCsp);
  assert.equal(res.headers.get("x-content-type-options"), "nosniff");
  assert.equal(res.headers.get("access-control-allow-origin"), null);
  assert.equal(res.headers.get("cache-control"), "public, max-age=300");
  assert.equal(res.headers.get("x-index-generated-at"), INDEX.generated_at);
  assert.equal(res.headers.get("x-index-db-sha256"), INDEX.db_sha256);
  assert.equal(res.headers.get("referrer-policy"), "no-referrer");
  assert.equal(res.headers.get("permissions-policy"), null);
});

test("HTML headers: HEAD / returns the same status and headers with no body", { timeout: 10000 }, async () => {
  const worker = makeWorker(INDEX, LEDGER, OFFER_SHAPE);
  const res = await worker.fetch(req("/", { method: "HEAD", headers: { accept: CHROME_ACCEPT } }), ALWAYS_ALLOW);
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("content-type"), "text/html; charset=utf-8");
  assert.equal(res.headers.get("vary"), "Accept");
  const text = await res.text();
  assert.equal(text, "");
});

// --- 4. CSP hash pin ---------------------------------------------------------------------------

function extractTagContent(html, tagName) {
  const openMatch = html.match(new RegExp(`<${tagName}[^>]*>`));
  assert.ok(openMatch, `no <${tagName}> found in PAGE_HTML`);
  const startIdx = openMatch.index + openMatch[0].length;
  const closeIdx = html.indexOf(`</${tagName}>`, startIdx);
  assert.ok(closeIdx !== -1, `no closing </${tagName}> found in PAGE_HTML`);
  return html.slice(startIdx, closeIdx);
}

test("CSP hash pin: PAGE_STYLE_SHA256/PAGE_SCRIPT_SHA256 match the extracted tag contents", { timeout: 10000 }, () => {
  const styleContent = extractTagContent(PAGE_HTML, "style");
  const scriptContent = extractTagContent(PAGE_HTML, "script");
  assert.equal(styleContent, PAGE_STYLE);
  assert.equal(scriptContent, PAGE_SCRIPT);

  const recomputedStyleHash = createHash("sha256").update(styleContent, "utf8").digest("base64");
  const recomputedScriptHash = createHash("sha256").update(scriptContent, "utf8").digest("base64");
  const recomputedStyleHashFromConst = createHash("sha256").update(PAGE_STYLE, "utf8").digest("base64");
  const recomputedScriptHashFromConst = createHash("sha256").update(PAGE_SCRIPT, "utf8").digest("base64");

  assert.equal(
    PAGE_STYLE_SHA256,
    recomputedStyleHash,
    `PAGE_STYLE_SHA256 = "${recomputedStyleHash}"\nPAGE_SCRIPT_SHA256 = "${recomputedScriptHash}"`,
  );
  assert.equal(
    PAGE_SCRIPT_SHA256,
    recomputedScriptHash,
    `PAGE_STYLE_SHA256 = "${recomputedStyleHash}"\nPAGE_SCRIPT_SHA256 = "${recomputedScriptHash}"`,
  );
  assert.equal(PAGE_STYLE_SHA256, recomputedStyleHashFromConst);
  assert.equal(PAGE_SCRIPT_SHA256, recomputedScriptHashFromConst);
});

// --- 5. Page hygiene ---------------------------------------------------------------------------

test("page hygiene: exactly one <script and one <style", { timeout: 10000 }, () => {
  const scriptCount = (PAGE_HTML.match(/<script/g) || []).length;
  const styleCount = (PAGE_HTML.match(/<style/g) || []).length;
  assert.equal(scriptCount, 1);
  assert.equal(styleCount, 1);
});

test("page hygiene: no inline event handlers, javascript: URLs, or style= attributes", { timeout: 10000 }, () => {
  assert.equal(/\son[a-z]+\s*=/i.test(PAGE_HTML), false);
  assert.equal(PAGE_HTML.includes("javascript:"), false);
  assert.equal(PAGE_HTML.includes("style="), false);
});

test("page hygiene: every https:// or http:// literal starts with an allowed prefix", { timeout: 10000 }, () => {
  const ALLOWED_PREFIXES = [
    "https://github.com/djd39448/openagentsearch",
    "https://djd39448.github.io/openagentsearch/",
  ];
  const matches = PAGE_HTML.match(/https?:\/\/[^\s"'<>)]*/g) || [];
  for (const m of matches) {
    assert.ok(
      ALLOWED_PREFIXES.some((prefix) => m.startsWith(prefix)),
      `disallowed external URL literal: ${m}`,
    );
  }
});

test("page hygiene: no banned APIs/strings anywhere in PAGE_HTML", { timeout: 10000 }, () => {
  const BANNED = [
    "innerHTML",
    "outerHTML",
    "insertAdjacentHTML",
    "document.write",
    "eval(",
    "new Function",
    "createContextualFragment",
    "DOMParser",
    "localStorage",
    "sessionStorage",
    "indexedDB",
    "document.cookie",
    "setInterval",
    "import(",
    '<link rel="stylesheet"',
    "<img",
    "<iframe",
    "@import",
  ];
  for (const needle of BANNED) {
    assert.equal(PAGE_HTML.includes(needle), false, `PAGE_HTML must not contain: ${needle}`);
  }
  // A CSS/HTML resource-loading `url(` -- checked with a word boundary so it does not false-
  // positive on unrelated identifiers that merely contain the substring (e.g. `buildCurl(`,
  // `new URL(`).
  assert.equal(/(?<![A-Za-z0-9_])url\(/.test(PAGE_HTML), false, "PAGE_HTML must not contain a CSS/HTML url(...)");
});

test("page hygiene: PAGE_HTML byte size is within budget", { timeout: 10000 }, () => {
  assert.ok(Buffer.byteLength(PAGE_HTML) <= 40960, `PAGE_HTML is ${Buffer.byteLength(PAGE_HTML)} bytes`);
});

test("page hygiene: PAGE_SCRIPT builds DOM only via textContent/createElement", { timeout: 10000 }, () => {
  assert.ok(PAGE_SCRIPT.includes("textContent"));
  assert.ok(PAGE_SCRIPT.includes("createElement"));
});

test("page hygiene: required static markup and strings are present", { timeout: 10000 }, () => {
  const REQUIRED_SUBSTRINGS = [
    '<html lang="en">',
    '<meta charset="utf-8">',
    "<title>OpenAgentSearch inspector</title>",
    '<link rel="alternate" type="application/json" href="/?format=json">',
    '<section id="search">',
    '<section id="did">',
    '<section id="route">',
    '<section id="health">',
    "Agents get JSON on this URL; this page is the same routes, rendered. Raw JSON on every panel is byte-identical to what an agent receives.",
    "advisory",
    "candidates_reason",
    "ranking_reason",
    "never an endorsement",
    "mentions, not authorship",
    "never used",
    "0.0 by construction",
    "absence is not evidence",
  ];
  for (const s of REQUIRED_SUBSTRINGS) {
    assert.ok(PAGE_HTML.includes(s), `PAGE_HTML is missing required text: ${s}`);
  }
});

test("page hygiene: no backtick and no ${ inside PAGE_SCRIPT or PAGE_STYLE", { timeout: 10000 }, () => {
  assert.equal(PAGE_SCRIPT.includes("`"), false);
  assert.equal(PAGE_SCRIPT.includes("${"), false);
  assert.equal(PAGE_STYLE.includes("`"), false);
  assert.equal(PAGE_STYLE.includes("${"), false);
});

// --- 6. Other routes ignore Accept: text/html --------------------------------------------------

const OTHER_ROUTE_CASES = [
  { name: "/healthz", path: "/healthz" },
  { name: "/search?q=authentication", path: "/search?q=authentication" },
  { name: "/did/{did}", path: `/did/${NON_BURST_DID}` },
  { name: "/route?model_hash=m1", path: "/route?model_hash=m1" },
  { name: "/nope (404)", path: "/nope" },
  { name: "/index/manifest.json (302)", path: "/index/manifest.json" },
];

for (const c of OTHER_ROUTE_CASES) {
  test(`other routes ignore Accept: text/html -- ${c.name}`, { timeout: 10000 }, async () => {
    const worker = makeWorker(INDEX, LEDGER, OFFER_SHAPE);
    const res = await worker.fetch(req(c.path, { headers: { accept: CHROME_ACCEPT } }), ALWAYS_ALLOW);
    assert.equal(res.headers.get("content-type"), "application/json; charset=utf-8");
    assert.equal(res.headers.get("vary"), null);
  });
}

// --- 7. access-control-expose-headers -----------------------------------------------------------

test("access-control-expose-headers present with the exact value on every JSON response", { timeout: 10000 }, async () => {
  const worker = makeWorker(INDEX, LEDGER, OFFER_SHAPE);
  const EXPECTED =
    "x-index-generated-at, x-index-db-sha256, x-ledger-generated-at, cache-control, retry-after";

  const rootRes = await worker.fetch(req("/"), ALWAYS_ALLOW);
  assert.equal(rootRes.headers.get("access-control-expose-headers"), EXPECTED);

  const healthRes = await worker.fetch(req("/healthz"), ALWAYS_ALLOW);
  assert.equal(healthRes.headers.get("access-control-expose-headers"), EXPECTED);

  const searchRes = await worker.fetch(req("/search?q=x"), ALWAYS_ALLOW);
  assert.equal(searchRes.headers.get("access-control-expose-headers"), EXPECTED);

  const badRes = await worker.fetch(req("/search"), ALWAYS_ALLOW); // 400 missing_query
  assert.equal(badRes.status, 400);
  assert.equal(badRes.headers.get("access-control-expose-headers"), EXPECTED);

  const notFoundRes = await worker.fetch(req("/nope"), ALWAYS_ALLOW); // 404
  assert.equal(notFoundRes.status, 404);
  assert.equal(notFoundRes.headers.get("access-control-expose-headers"), EXPECTED);

  const refusing = fakeEnv(async () => ({ success: false }));
  const limitedRes = await worker.fetch(req("/search?q=x"), refusing); // 429
  assert.equal(limitedRes.status, 429);
  assert.equal(limitedRes.headers.get("access-control-expose-headers"), EXPECTED);

  const htmlRes = await worker.fetch(req("/", { headers: { accept: CHROME_ACCEPT } }), ALWAYS_ALLOW);
  assert.equal(htmlRes.headers.get("access-control-expose-headers"), null);
});

// --- 8. wantsHtml unit ---------------------------------------------------------------------------

for (const c of ACCEPT_CASES) {
  test(`wantsHtml() unit: ${c.name}`, { timeout: 10000 }, () => {
    const qs = c.format ? `?format=${c.format}` : "";
    const urlStr = `${BASE}/${qs}`;
    const headers = c.accept === undefined ? undefined : { accept: c.accept };
    const request = new Request(urlStr, { headers });
    const url = new URL(urlStr);
    assert.equal(wantsHtml(request, url), c.expect === "html");
  });
}
