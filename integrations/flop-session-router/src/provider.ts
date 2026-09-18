// OpenAgentSearchCandidateProvider: a MinerCandidateProvider (retardio73-boop/flop-session-router,
// commit dba6525554c4ea5965ef6dd23e93194736aa0ef3 / tag v0.1.3-alpha) backed by the OpenAgentSearch
// public DID reputation ledger (GET /did/{did} on https://openagentsearch.trustcoresystems.workers.dev).
//
// Facts this ledger observes, from signed public technocore.chat room messages: that a did:key
// identity exists, when it was first/last seen, how many distinct texts it posted, whether other
// non-burst identities addressed it, and whether it belongs to a first-seen burst (>= 50 new keys
// in one 60s window). It observes nothing about latency, success, price, hardware, model, or FLOP
// settlement, and nothing here verifies a signature or attributes an identity to an operator. This
// module never turns that reputation signal into telemetry, price or assurance data: see the
// package README "What it is NOT" section.
import type { MinerCandidate, RouteRequest } from "./types.js";
import { boundedGetJson } from "./http.js";
import { BindingDidMismatch, InvalidDid, OpenAgentSearchUnavailable } from "./errors.js";

// Copied, not derived, from retardio73-boop/flop-session-router src/providers.ts at commit
// dba6525554c4ea5965ef6dd23e93194736aa0ef3 (tag v0.1.3-alpha). Not exported from types.ts (which
// is kept verbatim), so it is redefined here structurally; anything assignable to this interface
// is assignable to their real MinerCandidateProvider.
export interface MinerCandidateProvider {
  name: string;
  candidates(request: RouteRequest): Promise<MinerCandidate[]>;
  snapshot?(): Promise<MinerCandidate[]>;
}

/** The only DID form this package ever binds or fetches: "did:key:z" + 1-120 base58 characters. */
export const DID_KEY_PATTERN = /^did:key:z[1-9A-HJ-NP-Za-km-z]{1,120}$/;

/** The subset of a GET /did/{did} 200 body this package relies on. Other keys pass through. */
export interface LedgerBody {
  did: string;
  burst: boolean;
  score: number;
  facts_used: unknown;
  facts: Record<string, unknown>;
  provenance: { ledger_generated_at: string; [k: string]: unknown };
}

export type LedgerLookup =
  | { status: "known"; body: LedgerBody; ledgerGeneratedAt: string }
  | { status: "unknown" };

export interface LedgerVersion {
  indexGeneratedAt: string;
  ledgerGeneratedAt: string;
  ledgerDids: number;
}

export interface LedgerSourceOptions {
  baseUrl?: string;
  fetch?: typeof globalThis.fetch;
  userAgent?: string;
  timeoutMs?: number;
  maxBytes?: number;
  cacheTtlMs?: number;
  concurrency?: number;
  now?: () => number;
  allowPrivateEndpoints?: boolean;
}

export const PROVIDER_VERSION = "0.1.0";

const DEFAULT_BASE_URL = "https://openagentsearch.trustcoresystems.workers.dev";

/** Upper bound on memoised DIDs per source; the oldest entry is evicted past it (facts() is open-ended). */
const MAX_CACHED_LOOKUPS = 4096;

function requirePositiveFinite(value: number): number {
  if (!Number.isFinite(value) || value < 1) {
    throw new Error("INVALID_OPTION");
  }
  return value;
}

function isIPv4Literal(hostname: string): boolean {
  return /^(\d{1,3}\.){3}\d{1,3}$/.test(hostname);
}

/**
 * Validates that baseUrl is an https URL, carries no credentials/query/fragment, and (unless
 * allowPrivateEndpoints) does not name localhost or an IP literal. Does NOT perform DNS
 * resolution or any network access -- this is a syntactic check only, so it cannot catch a public
 * hostname that resolves to a private address (DNS rebinding is out of scope here). Returns
 * baseUrl with exactly one trailing slash stripped, otherwise unchanged.
 */
function assertPublicHttpsBaseUrl(baseUrl: string, allowPrivateEndpoints: boolean): string {
  const parsed = new URL(baseUrl);
  if (parsed.protocol !== "https:") {
    throw new Error("BASE_URL_NOT_HTTPS");
  }
  if (parsed.username !== "" || parsed.password !== "" || parsed.search !== "" || parsed.hash !== "") {
    throw new Error("BASE_URL_NOT_PUBLIC");
  }
  if (!allowPrivateEndpoints) {
    const hostname = parsed.hostname.toLowerCase();
    const isLocalhost = hostname === "localhost" || hostname.endsWith(".localhost");
    const isBracketedIPv6 = hostname.startsWith("[") && hostname.endsWith("]");
    if (isLocalhost || isIPv4Literal(hostname) || isBracketedIPv6) {
      throw new Error("BASE_URL_NOT_PUBLIC");
    }
  }
  return baseUrl.endsWith("/") ? baseUrl.slice(0, -1) : baseUrl;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isLedgerBody(body: unknown, did: string): body is LedgerBody {
  if (!isRecord(body)) return false;
  if (body.did !== did) return false;
  if (typeof body.burst !== "boolean") return false;
  const provenance = body.provenance;
  if (!isRecord(provenance)) return false;
  if (typeof provenance.ledger_generated_at !== "string") return false;
  return true;
}

function isErrorBody(body: unknown): body is { error: string } {
  return isRecord(body) && typeof body.error === "string";
}

function cloneLookup(value: LedgerLookup): LedgerLookup {
  return structuredClone(value);
}

/**
 * Fetches and memoizes GET /did/{did} and GET /healthz against one OpenAgentSearch deployment.
 * Does NOT verify signatures, does NOT resolve DNS itself, and does NOT guarantee freshness beyond
 * cacheTtlMs -- a cached "known"/"unknown" answer can be up to cacheTtlMs stale. The memo holds at
 * most MAX_CACHED_LOOKUPS DIDs (oldest evicted first); it is a per-process convenience, not a store.
 */
export class OpenAgentSearchLedgerSource {
  readonly baseUrl: string;
  private readonly fetchImpl: typeof globalThis.fetch;
  private readonly userAgent: string;
  private readonly timeoutMs: number;
  private readonly maxBytes: number;
  private readonly cacheTtlMs: number;
  private readonly concurrency: number;
  private readonly now: () => number;

  private readonly lookupCache = new Map<string, { cachedAt: number; value: LedgerLookup }>();

  private remember(did: string, value: LedgerLookup): void {
    this.lookupCache.delete(did);
    if (this.lookupCache.size >= MAX_CACHED_LOOKUPS) {
      const oldest = this.lookupCache.keys().next().value;
      if (oldest !== undefined) this.lookupCache.delete(oldest);
    }
    this.lookupCache.set(did, { cachedAt: this.now(), value });
  }
  private readonly inflightLookups = new Map<string, Promise<LedgerLookup>>();
  private versionCache: { cachedAt: number; value: LedgerVersion | undefined } | undefined;

  private activeFetches = 0;
  private readonly waiters: Array<() => void> = [];

  constructor(options: LedgerSourceOptions = {}) {
    this.baseUrl = assertPublicHttpsBaseUrl(
      options.baseUrl ?? DEFAULT_BASE_URL,
      options.allowPrivateEndpoints ?? false,
    );
    this.fetchImpl = options.fetch ?? globalThis.fetch;
    this.userAgent = options.userAgent ?? `openagentsearch-flop-session-router-provider/${PROVIDER_VERSION}`;
    this.timeoutMs = requirePositiveFinite(options.timeoutMs ?? 5000);
    this.maxBytes = requirePositiveFinite(options.maxBytes ?? 512 * 1024);
    this.cacheTtlMs = requirePositiveFinite(options.cacheTtlMs ?? 60_000);
    this.concurrency = requirePositiveFinite(options.concurrency ?? 4);
    this.now = options.now ?? Date.now;
  }

  private acquireSlot(): Promise<void> {
    if (this.activeFetches < this.concurrency) {
      this.activeFetches++;
      return Promise.resolve();
    }
    return new Promise<void>((resolve) => {
      this.waiters.push(() => {
        this.activeFetches++;
        resolve();
      });
    });
  }

  private releaseSlot(): void {
    this.activeFetches--;
    const next = this.waiters.shift();
    if (next !== undefined) next();
  }

  /**
   * Resolves one DID against the ledger. Throws InvalidDid before any fetch if did does not
   * match DID_KEY_PATTERN. A "known" or "unknown" answer is cached for cacheTtlMs; failures are
   * NEVER cached. Concurrent lookups for the same DID share one in-flight fetch.
   */
  async lookup(did: string): Promise<LedgerLookup> {
    if (!DID_KEY_PATTERN.test(did)) {
      throw new InvalidDid(did);
    }

    const cached = this.lookupCache.get(did);
    if (cached !== undefined && this.now() - cached.cachedAt < this.cacheTtlMs) {
      return cloneLookup(cached.value);
    }

    const existing = this.inflightLookups.get(did);
    if (existing !== undefined) {
      return existing.then(cloneLookup);
    }

    const promise = this.performLookup(did);
    this.inflightLookups.set(did, promise);
    try {
      const result = await promise;
      return cloneLookup(result);
    } finally {
      this.inflightLookups.delete(did);
    }
  }

  private async performLookup(did: string): Promise<LedgerLookup> {
    await this.acquireSlot();
    try {
      const { status, headers, body } = await boundedGetJson(`${this.baseUrl}/did/${did}`, {
        fetch: this.fetchImpl,
        userAgent: this.userAgent,
        timeoutMs: this.timeoutMs,
        maxBytes: this.maxBytes,
      });

      if (status === 200) {
        // A 200 whose body is not JSON, or is JSON about another DID, is not evidence about this one.
        if (body === undefined || !isLedgerBody(body, did)) {
          throw new OpenAgentSearchUnavailable("bad_json");
        }
        const ledgerGeneratedAt = headers.get("x-ledger-generated-at") ?? body.provenance.ledger_generated_at;
        const result: LedgerLookup = { status: "known", body, ledgerGeneratedAt };
        this.remember(did, result);
        return result;
      }

      if (status === 404 && isErrorBody(body) && body.error === "unknown_did") {
        const result: LedgerLookup = { status: "unknown" };
        this.remember(did, result);
        return result;
      }

      if (status === 404 && isErrorBody(body) && body.error === "ledger_not_built") {
        throw new OpenAgentSearchUnavailable("ledger_not_built");
      }

      // Every other status (429, 403, 5xx, an unexpected 404 body, a non-JSON error page) is reported
      // by its status code; the body is not required to be JSON here.
      throw new OpenAgentSearchUnavailable(`http_${status}`);
    } finally {
      this.releaseSlot();
    }
  }

  /**
   * Reports the ledger/index generation this source is currently talking to. Never throws --
   * ANY failure or unexpected /healthz shape resolves to undefined. Successful answers are
   * cached for cacheTtlMs; failures are not.
   */
  async version(): Promise<LedgerVersion | undefined> {
    const cached = this.versionCache;
    if (cached !== undefined && this.now() - cached.cachedAt < this.cacheTtlMs) {
      return cached.value;
    }

    try {
      const { status, body } = await boundedGetJson(`${this.baseUrl}/healthz`, {
        fetch: this.fetchImpl,
        userAgent: this.userAgent,
        timeoutMs: this.timeoutMs,
        maxBytes: this.maxBytes,
      });
      if (status !== 200 || !isRecord(body)) {
        return undefined;
      }
      const ledger = body.ledger;
      if (
        typeof body.generated_at !== "string" ||
        !isRecord(ledger) ||
        typeof ledger.generated_at !== "string" ||
        typeof ledger.dids !== "number"
      ) {
        return undefined;
      }
      const result: LedgerVersion = {
        indexGeneratedAt: body.generated_at,
        ledgerGeneratedAt: ledger.generated_at,
        ledgerDids: ledger.dids,
      };
      this.versionCache = { cachedAt: this.now(), value: result };
      return result;
    } catch {
      return undefined;
    }
  }
}

export interface LedgerBinding {
  did: string;
  candidate: MinerCandidate;
}

export interface ProviderOptions {
  burstPolicy?: "annotate" | "exclude";
  onUnavailable?: "throw" | "empty";
}

/**
 * A MinerCandidateProvider that emits a candidate only for an operator-bound DID the ledger
 * currently answers 200 for -- mirroring ExplicitDiscoveryCandidateProvider's binding pattern:
 * unbound rows cannot invent a candidate. Does NOT rank, does NOT read request.requestId, and
 * is NOT model-aware: the Router's own eligibility gates (MODEL_MISMATCH etc.) remain the only
 * filter on constraints.modelId. Its only mutation is attaching EvidenceProvenance to a
 * capability that has none; it never sets telemetry, price or assurance.
 */
export class OpenAgentSearchCandidateProvider implements MinerCandidateProvider {
  readonly name = "openagentsearch-ledger";

  private readonly source: OpenAgentSearchLedgerSource;
  private readonly bindings: LedgerBinding[];
  private readonly burstPolicy: "annotate" | "exclude";
  private readonly onUnavailable: "throw" | "empty";
  private lastSnapshot: MinerCandidate[] = [];

  constructor(source: OpenAgentSearchLedgerSource, bindings: LedgerBinding[], options: ProviderOptions = {}) {
    const burstPolicy = options.burstPolicy ?? "annotate";
    if (burstPolicy !== "annotate" && burstPolicy !== "exclude") {
      throw new Error("INVALID_OPTION");
    }
    const onUnavailable = options.onUnavailable ?? "throw";
    if (onUnavailable !== "throw" && onUnavailable !== "empty") {
      throw new Error("INVALID_OPTION");
    }

    const seenDids = new Set<string>();
    const seenIds = new Set<string>();
    for (const binding of bindings) {
      if (!DID_KEY_PATTERN.test(binding.did)) {
        throw new InvalidDid(binding.did);
      }
      if (seenDids.has(binding.did)) {
        throw new Error("DUPLICATE_BINDING_DID");
      }
      seenDids.add(binding.did);

      const candidate = binding.candidate;
      if (typeof candidate.id !== "string" || candidate.id.length === 0) {
        throw new Error("INVALID_CANDIDATE");
      }
      if (seenIds.has(candidate.id)) {
        throw new Error("DUPLICATE_CANDIDATE_ID");
      }
      seenIds.add(candidate.id);

      const identityDid = candidate.identity?.did;
      if (identityDid !== undefined && identityDid !== binding.did) {
        throw new BindingDidMismatch(binding.did, identityDid);
      }
      if (!Array.isArray(candidate.capabilities)) {
        throw new Error("INVALID_CANDIDATE");
      }
    }

    this.source = source;
    this.bindings = structuredClone(bindings);
    this.burstPolicy = burstPolicy;
    this.onUnavailable = onUnavailable;
  }

  async candidates(request: RouteRequest): Promise<MinerCandidate[]> {
    void request; // accepted and ignored: the ledger is not model-aware; see class docstring

    const settled = await Promise.allSettled(
      this.bindings.map(async (binding) => ({ binding, lookup: await this.source.lookup(binding.did) })),
    );

    const failures = settled.filter(
      (entry): entry is PromiseRejectedResult => entry.status === "rejected",
    );
    if (failures.length > 0) {
      const unavailable = failures.find((entry) => entry.reason instanceof OpenAgentSearchUnavailable);
      if (unavailable !== undefined) {
        if (this.onUnavailable === "empty") {
          return [];
        }
        throw unavailable.reason;
      }
      // Any error type other than OpenAgentSearchUnavailable always rethrows, regardless of
      // onUnavailable.
      throw failures[0]!.reason;
    }

    const fulfilled = settled as Array<
      PromiseFulfilledResult<{ binding: LedgerBinding; lookup: LedgerLookup }>
    >;

    const emitted: MinerCandidate[] = [];
    for (const { value } of fulfilled) {
      const { binding, lookup } = value;
      if (lookup.status !== "known") continue;
      if (this.burstPolicy === "exclude" && lookup.body.burst === true) continue;

      const candidate = structuredClone(binding.candidate);
      this.annotate(candidate, lookup.ledgerGeneratedAt, binding.did);
      emitted.push(candidate);
    }

    emitted.sort((a, b) => a.id.localeCompare(b.id));
    this.lastSnapshot = structuredClone(emitted);
    return emitted;
  }

  private annotate(candidate: MinerCandidate, ledgerGeneratedAt: string, did: string): void {
    for (const capability of candidate.capabilities) {
      if (capability.provenance !== undefined) continue; // never overwrite operator provenance
      capability.provenance = {
        source: "openagentsearch",
        sourceVersion: `ledger@${ledgerGeneratedAt}`,
        evidenceRef: `${this.source.baseUrl}/did/${did}`,
        verificationState: "OBSERVED",
        coverage: "PARTIAL",
        observedAt: ledgerGeneratedAt,
      };
      if (capability.observedAt === undefined) {
        capability.observedAt = ledgerGeneratedAt;
      }
    }
  }

  /** The candidates emitted by the last candidates() call, deep-cloned. [] before the first call. */
  async snapshot(): Promise<MinerCandidate[]> {
    return structuredClone(this.lastSnapshot);
  }

  /**
   * Advisory pass-through to source.lookup(did). This is evidence from one public message log,
   * never used by the Router itself, and never an endorsement of the identity it describes.
   */
  async facts(did: string): Promise<LedgerLookup> {
    return this.source.lookup(did);
  }
}
