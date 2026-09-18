// journey/run.mjs — a routing journey that exercises OpenAgentSearchCandidateProvider against the
// pinned flop-session-router (retardio73-boop/flop-session-router @ dba6525554c4ea5965ef6dd23e93194736aa0ef3),
// using this package's own fixtures as the only source of ledger data: no network, no wallet, no
// identity, no key. See ../PINS.md for the pins this run checks, and ../README.md "Journey evidence
// (CI)" for what J1-J7 prove and how to reproduce this locally or read the CI artifact.
//
// Plain ESM, Node built-ins only. Run after `npm run build` (see package.json "check").
//
// @typedef {{step: string, expected: unknown, actual: unknown, ok: boolean}} JourneyCheck

import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync } from "node:fs";
import { mkdir, readdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PKG = path.resolve(HERE, "..");
const REPO = path.resolve(PKG, "..", "..");
const VENDOR = path.join(REPO, "vendor", "flop-session-router");
const OUT = path.join(HERE, "out");

const EXPECTED_COMMIT = "dba6525554c4ea5965ef6dd23e93194736aa0ef3";
const FIXED_CLOCK_ISO = "2026-09-18T15:00:00Z";
const FIXED_CLOCK_MS = Date.parse(FIXED_CLOCK_ISO);
const BASE_URL = "https://openagentsearch.trustcoresystems.workers.dev";
const GIT_TIMEOUT_MS = 10_000;

/** @type {JourneyCheck[]} */
const checks = [];

/**
 * Records one journey assertion. Equality is by JSON serialization, which is exactly the
 * granularity these checks need (plain data: strings, arrays of ids, plain objects).
 * @param {string} step
 * @param {unknown} expected
 * @param {unknown} actual
 */
function check(step, expected, actual) {
  const ok = JSON.stringify(expected) === JSON.stringify(actual);
  checks.push({ step, expected, actual, ok });
  return ok;
}

/**
 * A NO-NETWORK fetch stub. `routes` maps a URL pathname to a canned response; any other pathname
 * (or an unmapped /did/... path) answers 500 {"error":"journey_stub"}. Every request URL that
 * reaches this stub is appended to the shared `requests` array (the J7 evidence).
 * @param {Record<string, {status: number, body: string, headers?: Record<string,string>}>} routes
 * @param {string[]} requests
 */
function makeStub(routes, requests) {
  return async function fetchStub(input) {
    const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
    requests.push(url);
    const pathname = new URL(url).pathname;
    const route = routes[pathname];
    if (route === undefined) {
      return new Response(JSON.stringify({ error: "journey_stub" }), {
        status: 500,
        headers: { "content-type": "application/json; charset=utf-8" },
      });
    }
    return new Response(route.body, {
      status: route.status,
      headers: route.headers ?? { "content-type": "application/json; charset=utf-8" },
    });
  };
}

/**
 * @param {string} name
 * @param {unknown} data
 */
async function writeJson(name, data) {
  await writeFile(path.join(OUT, name), JSON.stringify(data, null, 2) + "\n", "utf8");
}

/**
 * @param {string} cwd
 * @returns {string | null}
 */
function gitHeadOrNull(cwd) {
  try {
    return execFileSync("git", ["-C", cwd, "rev-parse", "HEAD"], {
      timeout: GIT_TIMEOUT_MS,
      encoding: "utf8",
    }).trim();
  } catch {
    return null;
  }
}

async function main() {
  const vendorEntry = path.join(VENDOR, "dist", "src", "index.js");
  const pkgEntry = path.join(PKG, "dist", "src", "index.js");
  if (!existsSync(vendorEntry)) {
    console.error("build the pinned router first");
    process.exit(2);
    return;
  }
  if (!existsSync(pkgEntry)) {
    console.error("run npm run build first");
    process.exit(2);
    return;
  }

  // --- verify the vendor pin -------------------------------------------------------------------
  const expectedCommit = process.env.FLOP_SESSION_ROUTER_COMMIT ?? EXPECTED_COMMIT;
  let commitVerified = false;
  if (existsSync(path.join(VENDOR, ".git"))) {
    let actualHead;
    try {
      actualHead = execFileSync("git", ["-C", VENDOR, "rev-parse", "HEAD"], {
        timeout: GIT_TIMEOUT_MS,
        encoding: "utf8",
      }).trim();
    } catch (err) {
      console.error(`failed to read vendor HEAD: ${err instanceof Error ? err.message : String(err)}`);
      process.exit(3);
      return;
    }
    if (actualHead !== expectedCommit) {
      console.error(`vendor commit mismatch: expected ${expectedCommit}, got ${actualHead}`);
      process.exit(3);
      return;
    }
    commitVerified = true;
  } else {
    console.warn("WARNING: vendor/flop-session-router/.git is absent; commit not verified (tarball checkout?)");
  }

  const vendorPkgJson = JSON.parse(await readFile(path.join(VENDOR, "package.json"), "utf8"));
  const ourPkgJson = JSON.parse(await readFile(path.join(PKG, "package.json"), "utf8"));

  const vendorMod = await import(pathToFileURL(vendorEntry).href);
  const { SessionRouter, RouterRepository, ROUTER_VERSION, DEFAULT_CONFIG } = vendorMod;
  const pkgMod = await import(pathToFileURL(pkgEntry).href);
  const { OpenAgentSearchLedgerSource, OpenAgentSearchCandidateProvider, OpenAgentSearchUnavailable } = pkgMod;

  // --- fixtures: load bytes and sha256 every one, sorted --------------------------------------
  const fixturesDir = path.join(PKG, "fixtures");
  const fixtureNames = (await readdir(fixturesDir)).filter((f) => f.endsWith(".json")).sort();
  /** @type {Record<string, Buffer>} */
  const fixtureBytes = {};
  /** @type {Record<string, string>} */
  const fixtures = {};
  for (const name of fixtureNames) {
    const bytes = await readFile(path.join(fixturesDir, name));
    fixtureBytes[name] = bytes;
    fixtures[`fixtures/${name}`] = createHash("sha256").update(bytes).digest("hex");
  }

  await mkdir(OUT, { recursive: true });

  const bindingsRaw = JSON.parse(fixtureBytes["bindings.example.json"].toString("utf8"));
  /** @type {Array<{did: string, candidate: import("../src/types.js").MinerCandidate}>} */
  const bindings = bindingsRaw.bindings;
  const oasBinding = bindings[0];
  const unknownBinding = bindings[1];
  const oasDid = oasBinding.did;
  const unknownDid = unknownBinding.did;

  const didOasBody = fixtureBytes["did-oas.json"].toString("utf8");
  const didOasHeaders = JSON.parse(fixtureBytes["did-oas.headers.json"].toString("utf8"));
  const didUnknownBody = fixtureBytes["did-unknown.json"].toString("utf8");
  const healthzBody = fixtureBytes["healthz.json"].toString("utf8");
  const ledgerGeneratedAt = didOasHeaders["x-ledger-generated-at"];

  const didOasPath = `/did/${oasDid}`;
  const didUnknownPath = `/did/${unknownDid}`;

  /** @type {string[]} */
  const requests = [];
  const fixedClock = () => new Date(FIXED_CLOCK_ISO);

  // ------------------------------------------------------------------------------------------
  // J1: select — both bindings resolved against the live-shaped fixtures; only the known DID is
  // admitted, and its capability gains exactly the six provenance fields.
  // ------------------------------------------------------------------------------------------
  const stub1 = makeStub(
    {
      [didOasPath]: {
        status: 200,
        body: didOasBody,
        headers: { "content-type": "application/json; charset=utf-8", "x-ledger-generated-at": ledgerGeneratedAt },
      },
      [didUnknownPath]: { status: 404, body: didUnknownBody },
      "/healthz": { status: 200, body: healthzBody },
    },
    requests,
  );
  const source1 = new OpenAgentSearchLedgerSource({ fetch: stub1, now: () => FIXED_CLOCK_MS });
  const provider1 = new OpenAgentSearchCandidateProvider(source1, bindings);
  const router1 = new SessionRouter(provider1, new RouterRepository(":memory:"), DEFAULT_CONFIG, fixedClock);

  const decision1 = await router1.route({ requestId: "journey-1", constraints: { modelId: "example-model" } });

  check("J1 status === SELECTED", "SELECTED", decision1.status);
  check("J1 ranking === [example-miner-oas]", ["example-miner-oas"], decision1.ranking);
  check("J1 selectedMinerId === example-miner-oas", "example-miner-oas", decision1.selectedMinerId);
  check(
    "J1 snapshot.candidates ids === [example-miner-oas] (unknown DID not admitted)",
    ["example-miner-oas"],
    decision1.snapshot.candidates.map((c) => c.id),
  );
  const admitted1 = decision1.snapshot.candidates.find((c) => c.id === "example-miner-oas");
  check(
    "J1 admitted capability provenance",
    {
      source: "openagentsearch",
      sourceVersion: `ledger@${ledgerGeneratedAt}`,
      evidenceRef: `${BASE_URL}${didOasPath}`,
      verificationState: "OBSERVED",
      coverage: "PARTIAL",
      observedAt: ledgerGeneratedAt,
    },
    admitted1?.capabilities?.[0]?.provenance,
  );
  check("J1 admitted telemetry is undefined", undefined, admitted1?.telemetry);
  check("J1 admitted price is undefined", undefined, admitted1?.price);
  check("J1 admitted assurance === UNKNOWN", "UNKNOWN", admitted1?.capabilities?.[0]?.assurance);

  await writeJson("decision.json", decision1);

  // ------------------------------------------------------------------------------------------
  // J2: replay — the stored evaluation recomputes to the same ranking under the same config hash.
  // ------------------------------------------------------------------------------------------
  const replay1 = router1.replay(decision1.decisionId);
  check("J2 replay status === REPLAY_MATCH", "REPLAY_MATCH", replay1.status);

  // ------------------------------------------------------------------------------------------
  // J3: snapshot — router.miners() reports the provider-scoped snapshot from the last candidates()
  // call (ids only compared; the snapshot is a deep clone, not the same object).
  // ------------------------------------------------------------------------------------------
  const minersResult = await router1.miners();
  check("J3 miners.status === provider_scoped", "provider_scoped", minersResult.status);
  check("J3 miners.provider === openagentsearch-ledger", "openagentsearch-ledger", minersResult.provider);
  check(
    "J3 miners ids === [example-miner-oas]",
    ["example-miner-oas"],
    minersResult.miners.map((m) => m.id),
  );

  // ------------------------------------------------------------------------------------------
  // J4: fail closed, empty — a fresh source (no memo) whose ledger answers 500 for every /did/
  // path, with onUnavailable: "empty", yields zero candidates and NO_ELIGIBLE_MINER (never a
  // partial list).
  // ------------------------------------------------------------------------------------------
  const stub500 = makeStub(
    {
      [didOasPath]: { status: 500, body: JSON.stringify({ error: "internal" }) },
      [didUnknownPath]: { status: 500, body: JSON.stringify({ error: "internal" }) },
    },
    requests,
  );
  const source500 = new OpenAgentSearchLedgerSource({ fetch: stub500, now: () => FIXED_CLOCK_MS });
  const providerEmpty = new OpenAgentSearchCandidateProvider(source500, bindings, { onUnavailable: "empty" });
  const routerEmpty = new SessionRouter(providerEmpty, new RouterRepository(":memory:"), DEFAULT_CONFIG, fixedClock);

  const decision4 = await routerEmpty.route({ requestId: "journey-2", constraints: { modelId: "example-model" } });
  check("J4 status === NO_ELIGIBLE_MINER", "NO_ELIGIBLE_MINER", decision4.status);
  check("J4 ranking === []", [], decision4.ranking);
  check("J4 snapshot.candidates === []", [], decision4.snapshot.candidates);

  await writeJson("decision-unavailable-empty.json", decision4);

  // ------------------------------------------------------------------------------------------
  // J5: fail closed, throw — the default provider (onUnavailable: "throw") over the SAME 500
  // source rejects route() outright; failures are never memoized, so this is a fresh fetch.
  // ------------------------------------------------------------------------------------------
  const providerThrow = new OpenAgentSearchCandidateProvider(source500, bindings);
  const routerThrow = new SessionRouter(providerThrow, new RouterRepository(":memory:"), DEFAULT_CONFIG, fixedClock);

  /** @type {unknown} */
  let j5Error;
  try {
    await routerThrow.route({ requestId: "journey-3", constraints: { modelId: "example-model" } });
  } catch (err) {
    j5Error = err;
  }
  check("J5 rejects with OpenAgentSearchUnavailable", true, j5Error instanceof OpenAgentSearchUnavailable);
  check("J5 error.message === OPENAGENTSEARCH_UNAVAILABLE", "OPENAGENTSEARCH_UNAVAILABLE", /** @type {any} */ (j5Error)?.message);
  check("J5 error.reason === http_500", "http_500", /** @type {any} */ (j5Error)?.reason);
  // No decision is persisted for J5: route() rejected before any persist() call, so there is
  // nothing to write to out/ for this step.

  // ------------------------------------------------------------------------------------------
  // J6: burst exclusion — a burst:true identity is excluded under burstPolicy "exclude" and
  // merely annotated (never scored) under the default "annotate". Single binding, as in this
  // package's own test (group4): a burst flag is exclusion or annotation, never a score, so no
  // second binding is needed to demonstrate it.
  // ------------------------------------------------------------------------------------------
  const didOasParsed = JSON.parse(didOasBody);
  const burstBody = JSON.stringify({ ...didOasParsed, burst: true });
  const burstRoute = {
    [didOasPath]: {
      status: 200,
      body: burstBody,
      headers: { "content-type": "application/json; charset=utf-8", "x-ledger-generated-at": ledgerGeneratedAt },
    },
  };

  const stubBurstExclude = makeStub(burstRoute, requests);
  const sourceBurstExclude = new OpenAgentSearchLedgerSource({ fetch: stubBurstExclude, now: () => FIXED_CLOCK_MS });
  const providerBurstExclude = new OpenAgentSearchCandidateProvider(sourceBurstExclude, [oasBinding], {
    burstPolicy: "exclude",
  });
  const routerBurstExclude = new SessionRouter(
    providerBurstExclude,
    new RouterRepository(":memory:"),
    DEFAULT_CONFIG,
    fixedClock,
  );
  const decision6a = await routerBurstExclude.route({ requestId: "journey-4", constraints: { modelId: "example-model" } });
  check("J6 exclude status === NO_ELIGIBLE_MINER", "NO_ELIGIBLE_MINER", decision6a.status);
  check("J6 exclude snapshot.candidates === []", [], decision6a.snapshot.candidates);

  await writeJson("decision-burst-exclude.json", decision6a);

  const stubBurstAnnotate = makeStub(burstRoute, requests);
  const sourceBurstAnnotate = new OpenAgentSearchLedgerSource({ fetch: stubBurstAnnotate, now: () => FIXED_CLOCK_MS });
  const providerBurstAnnotate = new OpenAgentSearchCandidateProvider(sourceBurstAnnotate, [oasBinding]);
  const routerBurstAnnotate = new SessionRouter(
    providerBurstAnnotate,
    new RouterRepository(":memory:"),
    DEFAULT_CONFIG,
    fixedClock,
  );
  const decision6b = await routerBurstAnnotate.route({ requestId: "journey-5", constraints: { modelId: "example-model" } });
  check("J6 annotate status === SELECTED", "SELECTED", decision6b.status);
  const annotated6b = decision6b.snapshot.candidates.find((c) => c.id === "example-miner-oas");
  check("J6 annotate assurance still UNKNOWN (a burst flag is never a score)", "UNKNOWN", annotated6b?.capabilities?.[0]?.assurance);

  await writeJson("decision-burst-annotate.json", decision6b);

  // ------------------------------------------------------------------------------------------
  // J7: no network — every recorded request stayed under the OpenAgentSearch base URL, and the
  // distinct set of paths fetched across the whole journey is exactly the two /did/{did} paths;
  // /healthz is defined in the J1 stub but never fetched, because version() is never called.
  // ------------------------------------------------------------------------------------------
  const allUnderBase = requests.every((u) => u.startsWith(`${BASE_URL}/`));
  check("J7 every request URL starts with the OpenAgentSearch base URL", true, allUnderBase);

  const distinctPaths = Array.from(new Set(requests.map((u) => new URL(u).pathname))).sort();
  const expectedPaths = [didOasPath, didUnknownPath].sort();
  check("J7 distinct paths fetched === {/did/<oas>, /did/<unknown>}", expectedPaths, distinctPaths);
  check("J7 /healthz was never fetched (version() never invoked)", false, distinctPaths.includes("/healthz"));

  // --- assemble and write the evidence bundle --------------------------------------------------
  const ok = checks.every((c) => c.ok);

  const ourCommit = process.env.GITHUB_SHA ?? gitHeadOrNull(REPO);

  const journey = {
    schema: "openagentsearch.flop-session-router-journey/1",
    generatedAt: new Date().toISOString(),
    fixedClock: FIXED_CLOCK_ISO,
    node: process.version,
    platform: process.platform,
    router: {
      repo: "retardio73-boop/flop-session-router",
      commit: expectedCommit,
      commitVerified,
      tag: "v0.1.3-alpha",
      packageVersion: vendorPkgJson.version,
      routerVersion: ROUTER_VERSION,
      configHash: decision1.configHash,
    },
    provider: {
      package: `${ourPkgJson.name}@${ourPkgJson.version}`,
      commit: ourCommit,
      name: provider1.name,
    },
    fixtures,
    requests,
    checks,
    ok,
  };

  await writeJson("journey.json", journey);
  await writeJson("pins.json", { router: journey.router, provider: journey.provider, fixtures: journey.fixtures });

  const readmeTxt = `flop-session-router-provider journey evidence
===============================================

This directory (journey/out/) is generated by journey/run.mjs. It records one run of the
OpenAgentSearchCandidateProvider against the pinned retardio73-boop/flop-session-router build,
driven entirely by this package's own fixtures (fixtures/*.json) -- no network, no wallet, no
identity, no key.

What varies between runs:
  - decisionId (the router's own randomUUID())
  - generatedAt (the real wall-clock time this run finished)

What is reproducible between runs (same fixtures, same pinned commit):
  - status, ranking, and REPLAY_MATCH for every decision
  - configHash (the router config is fixed: DEFAULT_CONFIG)
  - every fixture's sha256 (pins.json / journey.json "fixtures")
  - the J1-J7 check outcomes themselves

Regenerate from the repository root:

  git clone https://github.com/retardio73-boop/flop-session-router vendor/flop-session-router && git -C vendor/flop-session-router checkout dba6525554c4ea5965ef6dd23e93194736aa0ef3 && (cd vendor/flop-session-router && npm ci --ignore-scripts && npm run build) && (cd integrations/flop-session-router && npm ci --ignore-scripts && npm run check && node journey/run.mjs)

See ../../PINS.md for the exact pins and ../../README.md "Journey evidence (CI)" for what J1-J7 check.
`;
  await writeFile(path.join(OUT, "README.txt"), readmeTxt, "utf8");

  for (const c of checks) {
    console.log(`${c.ok ? "ok" : "FAIL"} - ${c.step}`);
  }
  console.log(`JOURNEY ok=${ok}`);

  process.exit(ok ? 0 : 1);
}

main().catch((err) => {
  console.error(err instanceof Error ? (err.stack ?? err.message) : String(err));
  process.exit(1);
});
