// Package C2b, spec Tests item 2: `search()` against every `tests/fixtures/lexical/queries.json`
// case, replayed over `tests/fixtures/lexical/fixture-index-v1.json`, plus focused tie-break /
// kind-filter / k-bounds / empty-query checks. Dependency-free (Windows Node or WSL).

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import path from "node:path";

import { search } from "../src/search.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, "..", "..");
const FIXTURES = path.join(REPO_ROOT, "tests", "fixtures", "lexical");

const INDEX = JSON.parse(readFileSync(path.join(FIXTURES, "fixture-index-v1.json"), "utf-8"));
const QUERIES = JSON.parse(readFileSync(path.join(FIXTURES, "queries.json"), "utf-8"));

test("queries.json has at least one case", () => {
  assert.ok(QUERIES.length > 0);
});

test("search() reproduces every queries.json case", async (t) => {
  for (const c of QUERIES) {
    await t.test(`${JSON.stringify(c.q)} k=${c.k} kind=${c.kind}`, () => {
      const results = search(INDEX, c.q, c.k, c.kind ?? null);
      assert.equal(results.length, c.hits.length);
      results.forEach((hit, i) => {
        assert.equal(hit.doc_sha256, c.hits[i].sha, `hit[${i}].doc_sha256`);
        assert.equal(hit.score, c.hits[i].score, `hit[${i}].score`);
      });
    });
  }
});

test("results carry the documented result shape", () => {
  const [hit] = search(INDEX, "authentication", 1);
  assert.deepEqual(Object.keys(hit).sort(), [
    "doc_sha256",
    "doc_url",
    "kind",
    "score",
    "section",
    "snippet",
    "title",
  ]);
});

test("tie-break orders equal scores by ascending doc index", () => {
  // "error" is common to every github_issue doc plus site[0..4] in the fixture build (see
  // scripts/make_lexical_fixture.py); several docs land at the exact same rounded score, so their
  // relative order in the result must follow their position in index.docs, not sha or url.
  const results = search(INDEX, "error", 20);
  const docIndexOf = new Map(INDEX.docs.map((doc, i) => [doc.sha, i]));
  for (let i = 1; i < results.length; i += 1) {
    const prev = results[i - 1];
    const cur = results[i];
    if (prev.score === cur.score) {
      assert.ok(
        docIndexOf.get(prev.doc_sha256) < docIndexOf.get(cur.doc_sha256),
        "equal-score results must be ordered by ascending doc index",
      );
    } else {
      assert.ok(prev.score > cur.score, "results must be ordered by descending score");
    }
  }
});

test("kind filter is applied before the top-k cut", () => {
  const unfiltered = search(INDEX, "integration", 10);
  assert.ok(unfiltered.length >= 2, "fixture must have >=2 unfiltered hits for this term");
  const filtered = search(INDEX, "integration", 10, "site");
  assert.ok(filtered.every((hit) => hit.kind === "site"));
  assert.ok(filtered.length < unfiltered.length);
});

test("k truncates the result set", () => {
  const results = search(INDEX, "error", 3);
  assert.equal(results.length, 3);
});

test("k below 1 throws RangeError", () => {
  assert.throws(() => search(INDEX, "authentication", 0), RangeError);
});

test("k above 50 throws RangeError", () => {
  assert.throws(() => search(INDEX, "authentication", 51), RangeError);
});

test("non-integer k throws RangeError", () => {
  assert.throws(() => search(INDEX, "authentication", 5.5), RangeError);
});

test("k bounds are checked even for an empty query", () => {
  assert.throws(() => search(INDEX, "", 0), RangeError);
  assert.throws(() => search(INDEX, "", 51), RangeError);
});

test("empty query returns no hits", () => {
  assert.deepEqual(search(INDEX, "", 10), []);
});

test("all-punctuation (tokenless) query returns no hits", () => {
  assert.deepEqual(search(INDEX, "!!!???", 10), []);
});

test("a term with no postings anywhere returns no hits", () => {
  assert.deepEqual(search(INDEX, "zzznonexistentzzz", 10), []);
});
