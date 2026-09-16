// Package C2b, spec Tests item 1: `tokenize()` against every shared fixture vector, every
// committed casefold-table entry, and the oversize-input bound. Dependency-free: imports nothing
// but `worker/src/search.js` and `node:*` built-ins, so it runs under plain Windows Node as well
// as inside WSL.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import path from "node:path";

import { MAX_INPUT_CHARS, casefoldCodePoint, tokenize } from "../src/search.js";
import CASEFOLD_TABLE from "../src/casefold-table.json" with { type: "json" };

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, "..", "..");
const VECTORS = JSON.parse(
  readFileSync(
    path.join(REPO_ROOT, "tests", "fixtures", "lexical", "tokenizer-vectors.json"),
    "utf-8",
  ),
);

test("tokenizer-vectors.json has at least one case", () => {
  assert.ok(VECTORS.length > 0);
});

test("tokenize() reproduces every tokenizer-vectors.json case", async (t) => {
  for (const vector of VECTORS) {
    await t.test(JSON.stringify(vector.input), () => {
      assert.deepEqual(tokenize(vector.input), vector.tokens);
    });
  }
});

test("every casefold-table entry folds exactly as the table states", () => {
  const entries = Object.entries(CASEFOLD_TABLE.map);
  assert.ok(entries.length > 0, "casefold table must not be empty");
  for (const [ch, folded] of entries) {
    assert.equal(
      casefoldCodePoint(ch),
      folded,
      `casefoldCodePoint(${JSON.stringify(ch)}) should equal ${JSON.stringify(folded)}`,
    );
  }
});

test("input over MAX_INPUT_CHARS code points throws RangeError", () => {
  const oversized = "a".repeat(MAX_INPUT_CHARS + 1);
  assert.throws(() => tokenize(oversized), RangeError);
});

test("input at exactly MAX_INPUT_CHARS code points does not throw", () => {
  const atLimit = "a".repeat(MAX_INPUT_CHARS);
  assert.doesNotThrow(() => tokenize(atLimit));
});

test("MAX_INPUT_CHARS bound counts Unicode code points, not UTF-16 code units", () => {
  // An astral (surrogate-pair) code point counts as ONE code point, so a string of
  // MAX_INPUT_CHARS astral characters (2 * MAX_INPUT_CHARS UTF-16 units) must not throw.
  const astral = "\u{1F600}".repeat(MAX_INPUT_CHARS); // emoji, non-word so tokens stay empty
  assert.doesNotThrow(() => tokenize(astral));
  assert.deepEqual(tokenize(astral), []);
});
