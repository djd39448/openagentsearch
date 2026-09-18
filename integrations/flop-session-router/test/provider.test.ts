import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

import {
  OpenAgentSearchCandidateProvider,
  OpenAgentSearchLedgerSource,
  OpenAgentSearchUnavailable,
  InvalidDid,
  BindingDidMismatch,
} from "../src/index.js";
import type { LedgerBinding, MinerCandidate, MinerCandidateProvider, UnavailableReason } from "../src/index.js";

const TIMEOUT = { timeout: 10_000 };

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

async function loadFixture(name: string): Promise<string> {
  const url = new URL("../../fixtures/" + name, import.meta.url);
  return readFile(url, "utf8");
}

async function loadBindingsFixture(): Promise<{ oasDid: string; unknownDid: string; bindings: LedgerBinding[] }> {
  const raw = JSON.parse(await loadFixture("bindings.example.json")) as { bindings: LedgerBinding[] };
  const first = raw.bindings[0];
  const second = raw.bindings[1];
  assert.ok(first);
  assert.ok(second);
  return { oasDid: first.did, unknownDid: second.did, bindings: structuredClone(raw.bindings) };
}

// Also guards the did-oas.headers.json sidecar against drifting from the did-oas.json body.
async function expectedLedgerGeneratedAt(): Promise<string> {
  const headers = JSON.parse(await loadFixture("did-oas.headers.json")) as Record<string, unknown>;
  const headerValue = headers["x-ledger-generated-at"];
  assert.equal(typeof headerValue, "string");
  const body = JSON.parse(await loadFixture("did-oas.json")) as { provenance: { ledger_generated_at: unknown } };
  assert.equal(body.provenance.ledger_generated_at, headerValue);
  return headerValue as string;
}

function makeCandidate(did: string, id: string): MinerCandidate {
  return {
    id,
    identity: { did },
    enabled: true,
    capabilities: [{ modelId: "m1", assurance: "UNKNOWN" }],
  };
}

function isUnavailable(reason: UnavailableReason) {
  return (err: unknown): boolean =>
    err instanceof OpenAgentSearchUnavailable &&
    err.reason === reason &&
    err.message === "OPENAGENTSEARCH_UNAVAILABLE";
}

interface RouteSpec {
  status: number;
  body: string;
  headers?: Record<string, string>;
}

interface StubCall {
  url: string;
  init: RequestInit;
}

type StubFetch = typeof globalThis.fetch & { calls: StubCall[] };

function toUrlString(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.toString();
  return input.url;
}

/** Standard fixture-route stub: `routes` maps a pathname to a canned response. */
function stubFetch(routes: Record<string, RouteSpec>): StubFetch {
  const calls: StubCall[] = [];
  const fn = async (input: RequestInfo | URL, init: RequestInit = {}): Promise<Response> => {
    const url = toUrlString(input);
    calls.push({ url, init });
    const pathname = new URL(url).pathname;
    const route = routes[pathname];
    if (route === undefined) {
      return new Response("boom", { status: 500 });
    }
    return new Response(route.body, { status: route.status, headers: route.headers });
  };
  return Object.assign(fn, { calls }) as StubFetch;
}

/** A fetch stub whose response never settles except by rejecting when the request is aborted. */
function abortAwareFetch(): StubFetch {
  const calls: StubCall[] = [];
  const fn = async (input: RequestInfo | URL, init: RequestInit = {}): Promise<Response> => {
    calls.push({ url: toUrlString(input), init });
    return new Promise<Response>((_resolve, reject) => {
      const signal = init.signal;
      const onAbort = (): void => {
        const err = new Error("The operation was aborted");
        err.name = "AbortError";
        reject(err);
      };
      if (signal === undefined || signal === null) return;
      if (signal.aborted) {
        onAbort();
        return;
      }
      signal.addEventListener("abort", onAbort, { once: true });
    });
  };
  return Object.assign(fn, { calls }) as StubFetch;
}

/** A 200 response whose body is a stream that throws the moment it is pulled from. */
function throwingBodyResponse(status: number, headers?: Record<string, string>): Response {
  const stream = new ReadableStream<Uint8Array>({
    pull() {
      throw new Error("body must not be read");
    },
  });
  return new Response(stream, { status, headers });
}

/** A 200 response streaming exactly `totalBytes` bytes, with no content-length header. */
function oversizeStreamResponse(totalBytes: number): Response {
  const chunkSize = 4096;
  let sent = 0;
  const stream = new ReadableStream<Uint8Array>({
    pull(controller) {
      const remaining = totalBytes - sent;
      if (remaining <= 0) {
        controller.close();
        return;
      }
      const size = Math.min(chunkSize, remaining);
      controller.enqueue(new Uint8Array(size));
      sent += size;
    },
  });
  return new Response(stream, { status: 200 });
}

function getInitHeader(init: RequestInit, name: string): string | undefined {
  const headers = init.headers;
  if (headers === undefined) return undefined;
  if (headers instanceof Headers) return headers.get(name) ?? undefined;
  if (Array.isArray(headers)) {
    const lower = name.toLowerCase();
    const found = headers.find(([key]) => key.toLowerCase() === lower);
    return found?.[1];
  }
  const record = headers as Record<string, string>;
  return record[name] ?? record[name.toLowerCase()];
}

const EIGHT_DID_SUFFIXES = ["a", "b", "c", "d", "e", "f", "g", "h"];
const EIGHT_DIDS = EIGHT_DID_SUFFIXES.map((suffix) => `did:key:z6Mktestbindingsuffix${suffix}wxyz`);

// ---------------------------------------------------------------------------
// Group 1: bound + known DID
// ---------------------------------------------------------------------------

test("group1: bound + known DID emits exactly the bound candidate, and results are isolated from mutation", TIMEOUT, async () => {
  const didOasBody = await loadFixture("did-oas.json");
  const { oasDid, bindings } = await loadBindingsFixture();
  const onlyOas = bindings.filter((b) => b.did === oasDid);
  const stub = stubFetch({ [`/did/${oasDid}`]: { status: 200, body: didOasBody } });
  const source = new OpenAgentSearchLedgerSource({ fetch: stub, now: () => 0, cacheTtlMs: 60_000 });
  const provider = new OpenAgentSearchCandidateProvider(source, onlyOas);
  const request = { requestId: "r1", constraints: { modelId: "example-model" } };

  const first = await provider.candidates(request);
  assert.equal(first.length, 1);
  const firstCandidate = first[0];
  assert.ok(firstCandidate);
  assert.equal(firstCandidate.id, "example-miner-oas");
  const ids = first.map((c) => c.id);
  assert.deepEqual(
    ids,
    [...ids].sort((a, b) => a.localeCompare(b)),
  );

  firstCandidate.capabilities.push({ modelId: "injected", assurance: "UNKNOWN" });
  firstCandidate.enabled = false;

  const second = await provider.candidates(request);
  const secondCandidate = second[0];
  assert.ok(secondCandidate);
  assert.equal(secondCandidate.capabilities.length, 1);
  assert.equal(secondCandidate.enabled, true);

  const originalBinding = onlyOas[0];
  assert.ok(originalBinding);
  assert.equal(originalBinding.candidate.capabilities.length, 1);
  assert.equal(originalBinding.candidate.enabled, true);
});

// ---------------------------------------------------------------------------
// Group 2: bound + unknown DID; unbound rows cannot invent a candidate
// ---------------------------------------------------------------------------

test("group2: unknown DID is not emitted, and unbound rows cannot invent a candidate", TIMEOUT, async () => {
  const didUnknownBody = await loadFixture("did-unknown.json");
  const { unknownDid, bindings } = await loadBindingsFixture();
  const onlyUnknown = bindings.filter((b) => b.did === unknownDid);
  const stub = stubFetch({ [`/did/${unknownDid}`]: { status: 404, body: didUnknownBody } });
  const source = new OpenAgentSearchLedgerSource({ fetch: stub, now: () => 0, cacheTtlMs: 60_000 });
  const provider = new OpenAgentSearchCandidateProvider(source, onlyUnknown);
  const request = { requestId: "r1", constraints: { modelId: "example-model" } };

  assert.deepEqual(await provider.candidates(request), []);

  // Zero bindings: no lookup is ever started, even though the ledger (stub) knows the DID.
  const knowingStub = stubFetch({ [`/did/${unknownDid}`]: { status: 200, body: await loadFixture("did-oas.json") } });
  const knowingSource = new OpenAgentSearchLedgerSource({ fetch: knowingStub, now: () => 0, cacheTtlMs: 60_000 });
  const emptyProvider = new OpenAgentSearchCandidateProvider(knowingSource, []);
  assert.deepEqual(await emptyProvider.candidates(request), []);
  assert.equal(knowingStub.calls.length, 0);
});

// ---------------------------------------------------------------------------
// Group 3: annotation is the only mutation
// ---------------------------------------------------------------------------

test("group3: annotation carries exact provenance and never overwrites existing evidence", TIMEOUT, async () => {
  const didOasBody = await loadFixture("did-oas.json");
  const ledgerGeneratedAt = await expectedLedgerGeneratedAt();
  const { oasDid } = await loadBindingsFixture();
  const request = { requestId: "r1", constraints: { modelId: "m1" } };

  const freshCandidate = makeCandidate(oasDid, "fresh");

  const stub = stubFetch({ [`/did/${oasDid}`]: { status: 200, body: didOasBody } });
  const source = new OpenAgentSearchLedgerSource({ fetch: stub, now: () => 0, cacheTtlMs: 60_000 });
  const provider = new OpenAgentSearchCandidateProvider(source, [{ did: oasDid, candidate: freshCandidate }]);
  const result = await provider.candidates(request);
  const emitted = result[0];
  assert.ok(emitted);
  const capability = emitted.capabilities[0];
  assert.ok(capability);
  assert.deepEqual(Object.keys(capability.provenance ?? {}), [
    "source",
    "sourceVersion",
    "evidenceRef",
    "verificationState",
    "coverage",
    "observedAt",
  ]);
  assert.deepEqual(capability.provenance, {
    source: "openagentsearch",
    sourceVersion: `ledger@${ledgerGeneratedAt}`,
    evidenceRef: `https://openagentsearch.trustcoresystems.workers.dev/did/${oasDid}`,
    verificationState: "OBSERVED",
    coverage: "PARTIAL",
    observedAt: ledgerGeneratedAt,
  });
  assert.equal(capability.observedAt, ledgerGeneratedAt);

  // A capability that already has provenance is left byte-identical, observedAt included.
  const operatorCandidate: MinerCandidate = {
    id: "operator-provenanced",
    identity: { did: oasDid },
    enabled: true,
    capabilities: [
      {
        modelId: "m1",
        assurance: "UNKNOWN",
        observedAt: "2020-01-01T00:00:00Z",
        provenance: { source: "operator", verificationState: "ASSERTED" },
      },
    ],
  };
  const operatorCapabilitySnapshot = JSON.stringify(operatorCandidate.capabilities[0]);
  const stub2 = stubFetch({ [`/did/${oasDid}`]: { status: 200, body: didOasBody } });
  const source2 = new OpenAgentSearchLedgerSource({ fetch: stub2, now: () => 0, cacheTtlMs: 60_000 });
  const provider2 = new OpenAgentSearchCandidateProvider(source2, [{ did: oasDid, candidate: operatorCandidate }]);
  const result2 = await provider2.candidates(request);
  const emitted2 = result2[0];
  assert.ok(emitted2);
  assert.equal(JSON.stringify(emitted2.capabilities[0]), operatorCapabilitySnapshot);

  // telemetry/price/assurance are read straight through, untouched.
  const telemetryCandidate: MinerCandidate = {
    id: "telemetry-price",
    identity: { did: oasDid },
    enabled: true,
    capabilities: [{ modelId: "m1", assurance: "UNKNOWN" }],
    telemetry: { successEwma: 0.9, latencyMsEwma: 120 },
    price: { amount: "1", asset: "FLOP", unit: "token", comparisonProfile: "p" },
  };
  const stub3 = stubFetch({ [`/did/${oasDid}`]: { status: 200, body: didOasBody } });
  const source3 = new OpenAgentSearchLedgerSource({ fetch: stub3, now: () => 0, cacheTtlMs: 60_000 });
  const provider3 = new OpenAgentSearchCandidateProvider(source3, [{ did: oasDid, candidate: telemetryCandidate }]);
  const result3 = await provider3.candidates(request);
  const emitted3 = result3[0];
  assert.ok(emitted3);
  assert.deepEqual(emitted3.telemetry, telemetryCandidate.telemetry);
  assert.deepEqual(emitted3.price, telemetryCandidate.price);
  assert.equal(emitted3.capabilities[0]?.assurance, "UNKNOWN");
});

// ---------------------------------------------------------------------------
// Group 4: burstPolicy
// ---------------------------------------------------------------------------

test("group4: burstPolicy excludes or annotates a burst:true identity", TIMEOUT, async () => {
  const didOasBody = JSON.parse(await loadFixture("did-oas.json")) as Record<string, unknown>;
  const burstBody = JSON.stringify({ ...didOasBody, burst: true });
  const { oasDid } = await loadBindingsFixture();
  const candidate = makeCandidate(oasDid, "burst-candidate");
  const request = { requestId: "r1", constraints: { modelId: "m1" } };

  const excludeStub = stubFetch({ [`/did/${oasDid}`]: { status: 200, body: burstBody } });
  const excludeSource = new OpenAgentSearchLedgerSource({ fetch: excludeStub, now: () => 0, cacheTtlMs: 60_000 });
  const excludeProvider = new OpenAgentSearchCandidateProvider(excludeSource, [{ did: oasDid, candidate }], {
    burstPolicy: "exclude",
  });
  assert.deepEqual(await excludeProvider.candidates(request), []);

  const annotateStub = stubFetch({ [`/did/${oasDid}`]: { status: 200, body: burstBody } });
  const annotateSource = new OpenAgentSearchLedgerSource({ fetch: annotateStub, now: () => 0, cacheTtlMs: 60_000 });
  const annotateProvider = new OpenAgentSearchCandidateProvider(annotateSource, [{ did: oasDid, candidate }]);
  const annotated = await annotateProvider.candidates(request);
  assert.equal(annotated.length, 1);
  const annotatedCandidate = annotated[0];
  assert.ok(annotatedCandidate);
  assert.ok(annotatedCandidate.capabilities[0]?.provenance);
  assert.equal(annotatedCandidate.capabilities[0]?.assurance, "UNKNOWN");
});

// ---------------------------------------------------------------------------
// Group 5: unavailability
// ---------------------------------------------------------------------------

test("group5a: ledger_not_built, http_500 and http_429 are mapped from status/body", TIMEOUT, async () => {
  const { oasDid } = await loadBindingsFixture();
  const request = { requestId: "r1", constraints: { modelId: "m1" } };
  const notBuiltBody = await loadFixture("did-not-built.json");
  const scenarios: Array<{ status: number; body: string; reason: UnavailableReason }> = [
    { status: 404, body: notBuiltBody, reason: "ledger_not_built" },
    { status: 500, body: JSON.stringify({ error: "internal" }), reason: "http_500" },
    { status: 429, body: JSON.stringify({ error: "rate_limited" }), reason: "http_429" },
  ];
  for (const scenario of scenarios) {
    const stub = stubFetch({ [`/did/${oasDid}`]: { status: scenario.status, body: scenario.body } });
    const source = new OpenAgentSearchLedgerSource({ fetch: stub, now: () => 0, cacheTtlMs: 60_000 });
    const provider = new OpenAgentSearchCandidateProvider(source, [{ did: oasDid, candidate: makeCandidate(oasDid, "c1") }]);
    await assert.rejects(provider.candidates(request), isUnavailable(scenario.reason));
  }
});

test("group5b: a request that never responds times out", TIMEOUT, async () => {
  const { oasDid } = await loadBindingsFixture();
  const stub = abortAwareFetch();
  const source = new OpenAgentSearchLedgerSource({ fetch: stub, now: () => 0, cacheTtlMs: 60_000, timeoutMs: 20 });
  const provider = new OpenAgentSearchCandidateProvider(source, [{ did: oasDid, candidate: makeCandidate(oasDid, "c1") }]);
  await assert.rejects(
    provider.candidates({ requestId: "r1", constraints: { modelId: "m1" } }),
    isUnavailable("timeout"),
  );
});

test("group5c: oversize is enforced both by content-length and by streamed byte count", TIMEOUT, async () => {
  const { oasDid } = await loadBindingsFixture();
  const request = { requestId: "r1", constraints: { modelId: "m1" } };

  const streamedStub: StubFetch = Object.assign(async () => oversizeStreamResponse(1025), { calls: [] }) as StubFetch;
  const streamedSource = new OpenAgentSearchLedgerSource({
    fetch: streamedStub,
    now: () => 0,
    cacheTtlMs: 60_000,
    maxBytes: 1024,
  });
  const streamedProvider = new OpenAgentSearchCandidateProvider(streamedSource, [
    { did: oasDid, candidate: makeCandidate(oasDid, "c1") },
  ]);
  await assert.rejects(streamedProvider.candidates(request), isUnavailable("oversize"));

  const declaredStub: StubFetch = Object.assign(
    async () => throwingBodyResponse(200, { "content-length": "2000" }),
    { calls: [] },
  ) as StubFetch;
  const declaredSource = new OpenAgentSearchLedgerSource({
    fetch: declaredStub,
    now: () => 0,
    cacheTtlMs: 60_000,
    maxBytes: 1024,
  });
  const declaredProvider = new OpenAgentSearchCandidateProvider(declaredSource, [
    { did: oasDid, candidate: makeCandidate(oasDid, "c1") },
  ]);
  await assert.rejects(declaredProvider.candidates(request), isUnavailable("oversize"));
});

test("group5d: malformed JSON and a body about a different DID both map to bad_json", TIMEOUT, async () => {
  const { oasDid, unknownDid } = await loadBindingsFixture();
  const request = { requestId: "r1", constraints: { modelId: "m1" } };

  const malformedStub = stubFetch({ [`/did/${oasDid}`]: { status: 200, body: "{not json" } });
  const malformedSource = new OpenAgentSearchLedgerSource({ fetch: malformedStub, now: () => 0, cacheTtlMs: 60_000 });
  const malformedProvider = new OpenAgentSearchCandidateProvider(malformedSource, [
    { did: oasDid, candidate: makeCandidate(oasDid, "c1") },
  ]);
  await assert.rejects(malformedProvider.candidates(request), isUnavailable("bad_json"));

  const wrongDidBody = JSON.parse(await loadFixture("did-oas.json")) as { did: string };
  wrongDidBody.did = unknownDid;
  const wrongDidStub = stubFetch({ [`/did/${oasDid}`]: { status: 200, body: JSON.stringify(wrongDidBody) } });
  const wrongDidSource = new OpenAgentSearchLedgerSource({ fetch: wrongDidStub, now: () => 0, cacheTtlMs: 60_000 });
  const wrongDidProvider = new OpenAgentSearchCandidateProvider(wrongDidSource, [
    { did: oasDid, candidate: makeCandidate(oasDid, "c1") },
  ]);
  await assert.rejects(wrongDidProvider.candidates(request), isUnavailable("bad_json"));
});

test("group5e: onUnavailable empty swallows a failure into []", TIMEOUT, async () => {
  const { oasDid } = await loadBindingsFixture();
  const stub = stubFetch({ [`/did/${oasDid}`]: { status: 500, body: JSON.stringify({ error: "internal" }) } });
  const source = new OpenAgentSearchLedgerSource({ fetch: stub, now: () => 0, cacheTtlMs: 60_000 });
  const provider = new OpenAgentSearchCandidateProvider(
    source,
    [{ did: oasDid, candidate: makeCandidate(oasDid, "c1") }],
    { onUnavailable: "empty" },
  );
  const result = await provider.candidates({ requestId: "r1", constraints: { modelId: "m1" } });
  assert.deepEqual(result, []);
});

test("group5f: an unavailable second binding never produces a partial list", TIMEOUT, async () => {
  const { oasDid, unknownDid } = await loadBindingsFixture();
  const didOasBody = await loadFixture("did-oas.json");
  const request = { requestId: "r1", constraints: { modelId: "m1" } };
  const bindings = [
    { did: oasDid, candidate: makeCandidate(oasDid, "a-known") },
    { did: unknownDid, candidate: makeCandidate(unknownDid, "b-fails") },
  ];

  const throwStub = stubFetch({
    [`/did/${oasDid}`]: { status: 200, body: didOasBody },
    [`/did/${unknownDid}`]: { status: 500, body: JSON.stringify({ error: "internal" }) },
  });
  const throwSource = new OpenAgentSearchLedgerSource({ fetch: throwStub, now: () => 0, cacheTtlMs: 60_000 });
  const throwProvider = new OpenAgentSearchCandidateProvider(throwSource, bindings);
  await assert.rejects(throwProvider.candidates(request), OpenAgentSearchUnavailable);

  const emptyStub = stubFetch({
    [`/did/${oasDid}`]: { status: 200, body: didOasBody },
    [`/did/${unknownDid}`]: { status: 500, body: JSON.stringify({ error: "internal" }) },
  });
  const emptySource = new OpenAgentSearchLedgerSource({ fetch: emptyStub, now: () => 0, cacheTtlMs: 60_000 });
  const emptyProvider = new OpenAgentSearchCandidateProvider(emptySource, bindings, { onUnavailable: "empty" });
  assert.deepEqual(await emptyProvider.candidates(request), []);
});

// ---------------------------------------------------------------------------
// Group 6: HTTP layer
// ---------------------------------------------------------------------------

test("group6: redirect refusal, https-only, private-endpoint refusal, UA and trailing-slash handling", TIMEOUT, async () => {
  const { oasDid } = await loadBindingsFixture();

  const redirectStub = stubFetch({
    [`/did/${oasDid}`]: { status: 302, body: "", headers: { location: "https://example.invalid/elsewhere" } },
  });
  const redirectSource = new OpenAgentSearchLedgerSource({ fetch: redirectStub, now: () => 0, cacheTtlMs: 60_000 });
  await assert.rejects(redirectSource.lookup(oasDid), isUnavailable("http_302"));
  assert.equal(redirectStub.calls.length, 1);

  assert.throws(() => new OpenAgentSearchLedgerSource({ baseUrl: "http://openagentsearch.example" }), /BASE_URL_NOT_HTTPS/);

  assert.throws(() => new OpenAgentSearchLedgerSource({ baseUrl: "https://127.0.0.1" }), /BASE_URL_NOT_PUBLIC/);
  assert.throws(() => new OpenAgentSearchLedgerSource({ baseUrl: "https://localhost" }), /BASE_URL_NOT_PUBLIC/);
  assert.doesNotThrow(() => new OpenAgentSearchLedgerSource({ baseUrl: "https://127.0.0.1", allowPrivateEndpoints: true }));
  assert.doesNotThrow(() => new OpenAgentSearchLedgerSource({ baseUrl: "https://localhost", allowPrivateEndpoints: true }));

  const uaStub = stubFetch({ [`/did/${oasDid}`]: { status: 200, body: await loadFixture("did-oas.json") } });
  const uaSource = new OpenAgentSearchLedgerSource({ fetch: uaStub, now: () => 0, cacheTtlMs: 60_000 });
  await uaSource.lookup(oasDid);
  assert.equal(uaStub.calls.length, 1);
  const call = uaStub.calls[0];
  assert.ok(call);
  assert.equal(getInitHeader(call.init, "user-agent"), "openagentsearch-flop-session-router-provider/0.1.0");
  assert.equal(call.init.redirect, "manual");

  const slashStub = stubFetch({ [`/did/${oasDid}`]: { status: 200, body: await loadFixture("did-oas.json") } });
  const slashSource = new OpenAgentSearchLedgerSource({
    baseUrl: "https://openagentsearch.trustcoresystems.workers.dev/",
    fetch: slashStub,
    now: () => 0,
    cacheTtlMs: 60_000,
  });
  await slashSource.lookup(oasDid);
  const slashCall = slashStub.calls[0];
  assert.ok(slashCall);
  assert.ok(!slashCall.url.includes("//did"));
});

// ---------------------------------------------------------------------------
// Group 7: invalid DIDs and binding validation
// ---------------------------------------------------------------------------

test("group7: invalid or mismatched DIDs and duplicate bindings are refused before any fetch", TIMEOUT, async () => {
  const { oasDid, unknownDid } = await loadBindingsFixture();

  const badDids = ["did:key:abc", "did:key:z0abc", "did:key:zOabc", "did:key:zIabc", "did:key:zlabc"];
  for (const badDid of badDids) {
    const stub = stubFetch({});
    const source = new OpenAgentSearchLedgerSource({ fetch: stub, now: () => 0 });
    assert.throws(
      () => new OpenAgentSearchCandidateProvider(source, [{ did: badDid, candidate: makeCandidate(badDid, "x") }]),
      InvalidDid,
    );
    assert.equal(stub.calls.length, 0);
  }

  const mismatchStub = stubFetch({});
  const mismatchSource = new OpenAgentSearchLedgerSource({ fetch: mismatchStub, now: () => 0 });
  const mismatchCandidate = makeCandidate(oasDid, "mismatched");
  mismatchCandidate.identity = { did: unknownDid };
  assert.throws(
    () => new OpenAgentSearchCandidateProvider(mismatchSource, [{ did: oasDid, candidate: mismatchCandidate }]),
    BindingDidMismatch,
  );

  const dupDidStub = stubFetch({});
  const dupDidSource = new OpenAgentSearchLedgerSource({ fetch: dupDidStub, now: () => 0 });
  assert.throws(
    () =>
      new OpenAgentSearchCandidateProvider(dupDidSource, [
        { did: oasDid, candidate: makeCandidate(oasDid, "a") },
        { did: oasDid, candidate: makeCandidate(oasDid, "b") },
      ]),
    /DUPLICATE_BINDING_DID/,
  );

  const dupIdStub = stubFetch({});
  const dupIdSource = new OpenAgentSearchLedgerSource({ fetch: dupIdStub, now: () => 0 });
  assert.throws(
    () =>
      new OpenAgentSearchCandidateProvider(dupIdSource, [
        { did: oasDid, candidate: makeCandidate(oasDid, "same-id") },
        { did: unknownDid, candidate: makeCandidate(unknownDid, "same-id") },
      ]),
    /DUPLICATE_CANDIDATE_ID/,
  );

  const factsStub = stubFetch({});
  const factsSource = new OpenAgentSearchLedgerSource({ fetch: factsStub, now: () => 0 });
  const factsProvider = new OpenAgentSearchCandidateProvider(factsSource, []);
  await assert.rejects(factsProvider.facts("not-a-did"), InvalidDid);
  assert.equal(factsStub.calls.length, 0);
});

// ---------------------------------------------------------------------------
// Group 8: memoization, in-flight sharing, bounded concurrency
// ---------------------------------------------------------------------------

test("group8: per-DID memo TTL, in-flight sharing, bounded concurrency, and no memo on failure", TIMEOUT, async () => {
  const { oasDid } = await loadBindingsFixture();
  const didOasBody = await loadFixture("did-oas.json");
  const request = { requestId: "r1", constraints: { modelId: "m1" } };

  let clock = 0;
  const memoStub = stubFetch({ [`/did/${oasDid}`]: { status: 200, body: didOasBody } });
  const memoSource = new OpenAgentSearchLedgerSource({ fetch: memoStub, now: () => clock, cacheTtlMs: 60_000 });
  const memoProvider = new OpenAgentSearchCandidateProvider(memoSource, [{ did: oasDid, candidate: makeCandidate(oasDid, "m") }]);

  await memoProvider.candidates(request);
  clock = 59_999;
  await memoProvider.candidates(request);
  assert.equal(memoStub.calls.length, 1);
  clock = 60_000;
  await memoProvider.candidates(request);
  assert.equal(memoStub.calls.length, 2);

  const sharedStub = stubFetch({ [`/did/${oasDid}`]: { status: 200, body: didOasBody } });
  const sharedSource = new OpenAgentSearchLedgerSource({ fetch: sharedStub, now: () => 0, cacheTtlMs: 60_000 });
  const sharedProvider = new OpenAgentSearchCandidateProvider(sharedSource, [
    { did: oasDid, candidate: makeCandidate(oasDid, "m") },
  ]);
  await Promise.all([sharedProvider.candidates(request), sharedProvider.candidates(request)]);
  assert.equal(sharedStub.calls.length, 1);

  let inFlight = 0;
  let maxInFlight = 0;
  const concurrencyFetch = async (input: RequestInfo | URL): Promise<Response> => {
    inFlight++;
    maxInFlight = Math.max(maxInFlight, inFlight);
    await new Promise<void>((resolve) => setTimeout(resolve, 5));
    inFlight--;
    const url = toUrlString(input);
    const did = url.slice(url.lastIndexOf("/") + 1);
    const parsed = JSON.parse(didOasBody) as Record<string, unknown>;
    return new Response(JSON.stringify({ ...parsed, did }), {
      status: 200,
      headers: { "x-ledger-generated-at": "2026-09-18T08:35:54Z" },
    });
  };
  const concurrencySource = new OpenAgentSearchLedgerSource({
    fetch: concurrencyFetch,
    now: () => 0,
    cacheTtlMs: 60_000,
    concurrency: 4,
  });
  const concurrencyBindings: LedgerBinding[] = EIGHT_DIDS.map((did, i) => ({
    did,
    candidate: makeCandidate(did, `c${i}`),
  }));
  const concurrencyProvider = new OpenAgentSearchCandidateProvider(concurrencySource, concurrencyBindings);
  await concurrencyProvider.candidates(request);
  // Eight bindings, four slots, every fetch parked on a timer: the semaphore must reach exactly 4.
  assert.equal(maxInFlight, 4);

  let attempt = 0;
  const flakyFetch = async (): Promise<Response> => {
    attempt++;
    if (attempt === 1) {
      return new Response(JSON.stringify({ error: "internal" }), { status: 500 });
    }
    return new Response(didOasBody, { status: 200, headers: { "x-ledger-generated-at": "2026-09-18T08:35:54Z" } });
  };
  const flakySource = new OpenAgentSearchLedgerSource({ fetch: flakyFetch, now: () => 0, cacheTtlMs: 60_000 });
  await assert.rejects(flakySource.lookup(oasDid), OpenAgentSearchUnavailable);
  const secondAttempt = await flakySource.lookup(oasDid);
  assert.equal(secondAttempt.status, "known");
  assert.equal(attempt, 2);
});

// ---------------------------------------------------------------------------
// Group 9: compile-time assignability and version()
// ---------------------------------------------------------------------------

test("group9: OpenAgentSearchCandidateProvider is a MinerCandidateProvider, and version() reports or fails closed", TIMEOUT, async () => {
  const { oasDid, bindings } = await loadBindingsFixture();
  const stub = stubFetch({ [`/did/${oasDid}`]: { status: 200, body: await loadFixture("did-oas.json") } });
  const source = new OpenAgentSearchLedgerSource({ fetch: stub, now: () => 0, cacheTtlMs: 60_000 });
  const p: MinerCandidateProvider = new OpenAgentSearchCandidateProvider(
    source,
    bindings.filter((b) => b.did === oasDid),
  );
  assert.equal(p.name, "openagentsearch-ledger");

  const healthzStub = stubFetch({ "/healthz": { status: 200, body: await loadFixture("healthz.json") } });
  const healthzSource = new OpenAgentSearchLedgerSource({ fetch: healthzStub, now: () => 0, cacheTtlMs: 60_000 });
  const version = await healthzSource.version();
  assert.deepEqual(version, {
    indexGeneratedAt: "2026-09-18T08:35:45Z",
    ledgerGeneratedAt: "2026-09-18T08:35:54Z",
    ledgerDids: 12605,
  });

  const failingStub = stubFetch({ "/healthz": { status: 500, body: JSON.stringify({ error: "internal" }) } });
  const failingSource = new OpenAgentSearchLedgerSource({ fetch: failingStub, now: () => 0, cacheTtlMs: 60_000 });
  await assert.doesNotReject(async () => {
    const failingVersion = await failingSource.version();
    assert.equal(failingVersion, undefined);
  });
});

// ---------------------------------------------------------------------------
// Orchestrator additions (after the draft): header precedence, non-JSON error bodies, stalled body
// ---------------------------------------------------------------------------

test("group3b: X-Ledger-Generated-At takes precedence over the body's provenance value", TIMEOUT, async () => {
  const { oasDid } = await loadBindingsFixture();
  const didOasBody = await loadFixture("did-oas.json");
  const headerValue = "2026-09-18T09:00:00Z";
  const stub = stubFetch({
    [`/did/${oasDid}`]: { status: 200, body: didOasBody, headers: { "x-ledger-generated-at": headerValue } },
  });
  const source = new OpenAgentSearchLedgerSource({ fetch: stub, now: () => 0, cacheTtlMs: 60_000 });
  const provider = new OpenAgentSearchCandidateProvider(source, [{ did: oasDid, candidate: makeCandidate(oasDid, "h") }]);
  const [emitted] = await provider.candidates({ requestId: "r1", constraints: { modelId: "m1" } });
  assert.ok(emitted);
  const capability = emitted.capabilities[0];
  assert.ok(capability);
  assert.equal(capability.observedAt, headerValue);
  assert.equal(capability.provenance?.observedAt, headerValue);
  assert.equal(capability.provenance?.sourceVersion, `ledger@${headerValue}`);
  const lookup = await source.lookup(oasDid);
  assert.equal(lookup.status, "known");
  if (lookup.status === "known") {
    assert.equal(lookup.ledgerGeneratedAt, headerValue);
    assert.equal(lookup.body.provenance.ledger_generated_at, "2026-09-18T08:35:54Z");
  }
});

test("group5g: a non-JSON error page is reported by its status, not as bad_json", TIMEOUT, async () => {
  const { oasDid } = await loadBindingsFixture();
  const cases: Array<{ status: number; reason: UnavailableReason }> = [
    { status: 500, reason: "http_500" },
    { status: 403, reason: "http_403" },
    { status: 404, reason: "http_404" },
  ];
  for (const c of cases) {
    const stub = stubFetch({ [`/did/${oasDid}`]: { status: c.status, body: "<html>error</html>" } });
    const source = new OpenAgentSearchLedgerSource({ fetch: stub, now: () => 0, cacheTtlMs: 60_000 });
    await assert.rejects(source.lookup(oasDid), isUnavailable(c.reason));
  }
});

test("group5h: a body stream that stalls after the headers still times out", TIMEOUT, async () => {
  const { oasDid } = await loadBindingsFixture();
  const stalledFetch = async (): Promise<Response> => {
    const stream = new ReadableStream<Uint8Array>({
      pull() {
        return new Promise<void>(() => {
          /* never enqueues, never closes */
        });
      },
    });
    return new Response(stream, { status: 200 });
  };
  const source = new OpenAgentSearchLedgerSource({ fetch: stalledFetch, now: () => 0, cacheTtlMs: 60_000, timeoutMs: 20 });
  await assert.rejects(source.lookup(oasDid), isUnavailable("timeout"));
});
