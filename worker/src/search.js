// The `unicode-word-casefold-v1` tokenizer and the reference BM25 ranking, ported from
// `openagentsearch.lexical.tokenize` / `openagentsearch.lexical.search` (Python) so this Worker
// can rank queries against a precomputed `lexical-v1.json` index with no server of its own.
//
// Pure functions only: no I/O, no globals, no `fetch`, no `eval`. The two suites that pin this
// module's behavior are `tests/fixtures/lexical/tokenizer-vectors.json` (tokenize) and
// `tests/fixtures/lexical/queries.json` (search), both replayed by `worker/test/`.
//
// NOT guaranteed: byte-for-byte agreement with Python's `str.casefold()` / NFKC normalization for
// characters newer than the older of Node's ICU and CPython's `unicodedata` version bundled with
// this Worker's toolchain -- see `docs/api.md` for the Unicode-version caveat. The committed
// fixtures are the actual contract, not this comment.

import CASEFOLD_TABLE from "./casefold-table.json" with { type: "json" };

const MIN_TOKEN_LEN = 2;
const MAX_TOKEN_LEN = 40;

/** Mirrors `openagentsearch.lexical.tokenize.MAX_INPUT_CHARS`: the largest input `tokenize()`
 * will accept, measured in Unicode code points (not UTF-16 code units). */
export const MAX_INPUT_CHARS = 1_000_000;

const _MIN_K = 1;
const _MAX_K = 50;

const WORD_RUN = /[\p{L}\p{N}_]+/gu;

const CASEFOLD_MAP = CASEFOLD_TABLE.map;

/**
 * The number of Unicode code points in `text` (as opposed to `text.length`, which counts UTF-16
 * code units and over-counts every character outside the Basic Multilingual Plane).
 *
 * NOT guaranteed: this is O(n) in `text`'s length; callers on a hot path with an already-bounded
 * `text` (for example a validated ≤512-character query) do not need to call it themselves.
 *
 * @param {string} text
 * @returns {number}
 */
export function codePointLength(text) {
  let n = 0;
  // eslint-disable-next-line no-unused-vars
  for (const _ch of text) n += 1;
  return n;
}

/**
 * One already-NFKC-normalized Unicode code point (given as a one-character string) folded the
 * same way Python's `str.casefold()` would fold it: the committed casefold table's entry when
 * one exists (this is the ONLY case where Python's `casefold()` and `lower()` disagree for this
 * exact code point), otherwise `String.prototype.toLowerCase()`, which agrees with Python's
 * `str.lower()` for every other code point in both languages' shared Unicode data.
 *
 * NOT guaranteed: correctness for a multi-code-point grapheme cluster passed as `ch` -- this
 * function assumes `ch` is exactly one Unicode code point, which is how `casefold()` (below)
 * always calls it.
 *
 * @param {string} ch
 * @returns {string}
 */
export function casefoldCodePoint(ch) {
  const folded = CASEFOLD_MAP[ch];
  return folded !== undefined ? folded : ch.toLowerCase();
}

/**
 * `text` casefolded one Unicode code point at a time via {@link casefoldCodePoint}. Equivalent to
 * Python's `str.casefold()` for every code point the committed casefold table and `toLowerCase()`
 * agree on with Python's own `casefold()`/`lower()` pair -- see the module docstring for what is
 * NOT guaranteed here.
 *
 * @param {string} text
 * @returns {string}
 */
function casefold(text) {
  let out = "";
  for (const ch of text) out += casefoldCodePoint(ch);
  return out;
}

/**
 * One maximal word run (already normalized and casefolded) -> the tokens it contributes: the run
 * itself when its code-point length is in `[2, 40]`, followed by each of its `_`-separated parts
 * that independently passes the same length rule (a part identical to the whole run, which cannot
 * actually arise, is skipped rather than duplicated) -- mirrors
 * `openagentsearch.lexical.tokenize._emit_run` exactly.
 *
 * @param {string} run
 * @returns {string[]}
 */
function emitRun(run) {
  const tokens = [];
  const runLen = codePointLength(run);
  if (runLen >= MIN_TOKEN_LEN && runLen <= MAX_TOKEN_LEN) tokens.push(run);
  if (run.includes("_")) {
    for (const part of run.split("_")) {
      if (part === run) continue;
      const partLen = codePointLength(part);
      if (partLen >= MIN_TOKEN_LEN && partLen <= MAX_TOKEN_LEN) tokens.push(part);
    }
  }
  return tokens;
}

/**
 * `text` -> tokens, in order, duplicates preserved -- the JavaScript port of
 * `openagentsearch.lexical.tokenize.tokenize`. Pipeline: NFKC-normalize -> casefold each code
 * point ({@link casefoldChar}) -> split into maximal runs matching `/[\p{L}\p{N}_]/u` -> each run
 * through {@link emitRun}. Pure: no I/O, no randomness.
 *
 * NOT guaranteed: exact agreement with the Python reference for a code point whose Unicode
 * category or case-folding data changed between the two runtimes' bundled Unicode versions (see
 * `docs/api.md`); this says nothing about which terms exist in any particular index.
 *
 * @param {string} text
 * @returns {string[]}
 * @throws {RangeError} `text` is over {@link MAX_INPUT_CHARS} Unicode code points.
 */
export function tokenize(text) {
  if (codePointLength(text) > MAX_INPUT_CHARS) {
    throw new RangeError(
      `text is over the ${MAX_INPUT_CHARS}-code-point limit`,
    );
  }
  const folded = casefold(text.normalize("NFKC"));
  const runs = folded.match(WORD_RUN) || [];
  const tokens = [];
  for (const run of runs) tokens.push(...emitRun(run));
  return tokens;
}

/**
 * `tokenize(text)` reduced to distinct terms in first-seen order, cut at `maxTerms` -- the
 * JavaScript port of `openagentsearch.lexical.tokenize.query_terms`.
 *
 * NOT guaranteed: a term returned here may have no postings in any particular index.
 *
 * @param {string} text
 * @param {number} [maxTerms]
 * @returns {string[]}
 */
export function queryTerms(text, maxTerms = 32) {
  const seen = new Set();
  const distinct = [];
  for (const token of tokenize(text)) {
    if (!seen.has(token)) {
      seen.add(token);
      distinct.push(token);
    }
  }
  return distinct.slice(0, maxTerms);
}

/**
 * Compares two strings by Unicode code point (not by UTF-16 code unit, which misorders code
 * points outside the Basic Multilingual Plane relative to BMP code points above `U+DFFF`) --
 * agrees with Python's default string ordering, which `search()` (below) relies on for the
 * documented sorted-term-order summation.
 *
 * @param {string} a
 * @param {string} b
 * @returns {number}
 */
function compareCodePoints(a, b) {
  const ai = a[Symbol.iterator]();
  const bi = b[Symbol.iterator]();
  for (;;) {
    const an = ai.next();
    const bn = bi.next();
    if (an.done && bn.done) return 0;
    if (an.done) return -1;
    if (bn.done) return 1;
    const diff = an.value.codePointAt(0) - bn.value.codePointAt(0);
    if (diff !== 0) return diff;
  }
}

/**
 * One term's BM25 contribution to one document's score -- mirrors
 * `openagentsearch.lexical.search._bm25_term_score` exactly.
 *
 * @param {number} idf
 * @param {number} tf
 * @param {number} k1
 * @param {number} b
 * @param {number} lengthRatio
 * @returns {number}
 */
function bm25TermScore(idf, tf, k1, b, lengthRatio) {
  const denom = tf + k1 * (1 - b + b * lengthRatio);
  return (idf * tf * (k1 + 1)) / denom;
}

/**
 * Ranks `index.docs` against `q` by BM25 (`index.bm25.k1`, `index.bm25.b`) and returns the top
 * `k` -- the JavaScript port of `openagentsearch.lexical.search.search`, over the same
 * `lexical-v1.json` shape `openagentsearch.lexical.index.to_json_bytes` writes (`index.terms[term]`
 * is a list of `[docIndex, tf]` pairs sorted ascending by `docIndex`; `index.docs[i].len` is the
 * document's token length).
 *
 * `q` is reduced to at most 32 distinct terms via {@link queryTerms}; only terms present in
 * `index.terms` are scored, and their contributions are summed in **sorted term order** (by
 * Unicode code point, via {@link compareCodePoints}) -- not query order -- so the floating-point
 * total, and therefore the 6-decimal-place rounding below, matches the Python reference
 * regardless of how the query was phrased. Docs whose total score is exactly `0` after rounding
 * are excluded; `kind`, when given, is applied BEFORE the top-`k` cut, so a filtered query can
 * return fewer than `k` hits even when `k` unfiltered hits exist. Results are ordered by
 * `(-score, docIndex)`, so ties break deterministically by a document's position in `index.docs`.
 *
 * NOT guaranteed: `kind` is not validated against any fixed set of known kinds (an unknown `kind`
 * simply matches nothing); this function does not check whether `q` exceeds any caller-side
 * length bound (see `docs/api.md` for the Worker's own `q` length cap).
 *
 * @param {object} index a parsed `lexical-v1.json` document
 * @param {string} q
 * @param {number} k
 * @param {string | null} [kind]
 * @returns {Array<{doc_sha256: string, doc_url: string, title: string, section: string, kind: string, score: number, snippet: string}>}
 * @throws {RangeError} `k` is not an integer in `[1, 50]`.
 */
export function search(index, q, k, kind = null) {
  if (!Number.isInteger(k) || k < _MIN_K || k > _MAX_K) {
    throw new RangeError(`k must be an integer between ${_MIN_K} and ${_MAX_K}, got ${k}`);
  }

  const terms = queryTerms(q, 32);
  if (terms.length === 0) return [];

  const docCount = index.docs.length;
  const { k1, b } = index.bm25;
  const avgdl = index.avgdl;

  /** @type {Map<number, number>} */
  const rawScores = new Map();
  const sortedTerms = terms.slice().sort(compareCodePoints);
  for (const term of sortedTerms) {
    const postings = index.terms[term];
    if (!postings || postings.length === 0) continue;
    const df = postings.length;
    const idf = Math.log(1 + (docCount - df + 0.5) / (df + 0.5));
    for (const [docIndex, tf] of postings) {
      const doc = index.docs[docIndex];
      const lengthRatio = avgdl ? doc.len / avgdl : 0;
      const contribution = bm25TermScore(idf, tf, k1, b, lengthRatio);
      rawScores.set(docIndex, (rawScores.get(docIndex) || 0) + contribution);
    }
  }

  const scored = [];
  for (const [docIndex, rawScore] of rawScores) {
    const score = Math.round(rawScore * 1e6) / 1e6;
    if (score === 0) continue;
    const doc = index.docs[docIndex];
    if (kind !== null && doc.kind !== kind) continue;
    scored.push({ docIndex, score, doc });
  }

  scored.sort((x, y) => (x.score !== y.score ? y.score - x.score : x.docIndex - y.docIndex));

  return scored.slice(0, k).map(({ score, doc }) => ({
    doc_sha256: doc.sha,
    doc_url: doc.url,
    title: doc.title,
    section: doc.section,
    kind: doc.kind,
    score,
    snippet: doc.abstract,
  }));
}
