// Bounded, non-following, size-capped GET-JSON transport for this package's provider.
//
// This module owns ONE concern: fetch one URL once, refuse redirects, cap how much of the body
// is ever read, hold one deadline over the whole exchange (headers AND body), and turn every
// transport-level failure into an OpenAgentSearchUnavailable with a specific reason. It does NOT
// retry, does NOT follow redirects, and does NOT know anything about what a "safe" host looks
// like (private-address / localhost / IP-literal refusal lives in assertPublicHttpsBaseUrl in
// provider.ts, which runs before any URL built from a base ever reaches this module). It also
// does NOT validate the shape of a parsed JSON body — every status code is handed back to the
// caller, which is the only place that knows what a given endpoint's success/error bodies are
// supposed to look like.
import { OpenAgentSearchUnavailable } from "./errors.js";

/** Inputs for a single bounded GET request. */
export interface BoundedFetchOptions {
  fetch: typeof globalThis.fetch;
  userAgent: string;
  timeoutMs: number;
  maxBytes: number;
}

/** What one bounded GET yields. `body` is `undefined` when the bytes were not valid JSON. */
export interface BoundedJsonResponse {
  status: number;
  headers: Headers;
  body: unknown;
}

const REDIRECT_STATUSES = new Set([301, 302, 303, 307, 308]);

/**
 * Performs one bounded GET request against `url` and parses the response body as JSON.
 *
 * Does NOT guarantee `url`'s host is safe to reach (no private/loopback/IP-literal checks here).
 * Does NOT retry on any failure. Does NOT follow redirects: any 3xx status, or any response that
 * carries a `location` header, is treated as a failure rather than followed. Does NOT validate
 * that the parsed JSON has any particular shape — callers must narrow `body` themselves, and a
 * body that is not valid JSON comes back as `body: undefined` (JSON.parse can never yield
 * `undefined`, so the value is unambiguous) rather than as an error, because only the caller
 * knows whether a non-JSON body on a given status is a failure. The deadline (`timeoutMs`) covers
 * the whole exchange: waiting for headers and reading the body. Only timeout, network, redirect
 * and oversize failures throw, each as an OpenAgentSearchUnavailable carrying the matching reason.
 */
export async function boundedGetJson(url: string, opts: BoundedFetchOptions): Promise<BoundedJsonResponse> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), opts.timeoutMs);
  try {
    let response: Response;
    try {
      response = await opts.fetch(url, {
        method: "GET",
        headers: { "user-agent": opts.userAgent, accept: "application/json" },
        redirect: "manual",
        signal: controller.signal,
      });
    } catch (err) {
      throw controller.signal.aborted
        ? new OpenAgentSearchUnavailable("timeout", { cause: err })
        : new OpenAgentSearchUnavailable("network", { cause: err });
    }

    if (REDIRECT_STATUSES.has(response.status) || response.headers.has("location")) {
      throw new OpenAgentSearchUnavailable(`http_${response.status}`);
    }

    const contentLength = response.headers.get("content-length");
    if (contentLength !== null) {
      const declared = Number(contentLength);
      if (Number.isFinite(declared) && declared > opts.maxBytes) {
        throw new OpenAgentSearchUnavailable("oversize");
      }
    }

    let bytes: Uint8Array;
    try {
      bytes = await readBounded(response, opts.maxBytes, controller.signal);
    } catch (err) {
      if (err instanceof OpenAgentSearchUnavailable) throw err;
      throw controller.signal.aborted
        ? new OpenAgentSearchUnavailable("timeout", { cause: err })
        : new OpenAgentSearchUnavailable("network", { cause: err });
    }

    let body: unknown;
    try {
      body = JSON.parse(new TextDecoder().decode(bytes));
    } catch {
      body = undefined;
    }
    return { status: response.status, headers: response.headers, body };
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Reads at most `maxBytes` of `response`'s body. Stops (cancels the reader) and throws `oversize`
 * the moment the running total exceeds the cap. Reacts to `signal` between chunks so a stalled
 * stream cannot outlive the caller's deadline; a `fetch` implementation that honours the signal
 * itself will also reject the pending read.
 */
async function readBounded(response: Response, maxBytes: number, signal: AbortSignal): Promise<Uint8Array> {
  const aborted = new Promise<never>((_resolve, reject) => {
    const onAbort = (): void => reject(signal.reason instanceof Error ? signal.reason : new Error("aborted"));
    if (signal.aborted) onAbort();
    else signal.addEventListener("abort", onAbort, { once: true });
  });

  if (response.body === null) {
    const buffer = new Uint8Array(await Promise.race([response.arrayBuffer(), aborted]));
    if (buffer.byteLength > maxBytes) {
      throw new OpenAgentSearchUnavailable("oversize");
    }
    return buffer;
  }

  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    for (;;) {
      const { done, value } = await Promise.race([reader.read(), aborted]);
      if (done) break;
      if (value === undefined) continue;
      total += value.byteLength;
      if (total > maxBytes) {
        throw new OpenAgentSearchUnavailable("oversize");
      }
      chunks.push(value);
    }
  } catch (err) {
    try {
      await reader.cancel();
    } catch {
      // best-effort cancel; the failure below is what matters
    }
    throw err;
  }

  const out = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    out.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return out;
}
