// Error types shared by the transport (http.ts) and the ledger source / provider (provider.ts).
// Kept in their own module so neither of those two imports the other.

export type UnavailableReason =
  | "ledger_not_built"
  | `http_${number}`
  | "timeout"
  | "oversize"
  | "bad_json"
  | "network";

/**
 * Thrown for every transport- or ledger-level failure that this package cannot resolve into a
 * definite "known" or "unknown" answer. Does NOT distinguish a transient failure from a permanent
 * one beyond what `reason` says, and does NOT retry anything itself.
 */
export class OpenAgentSearchUnavailable extends Error {
  readonly reason: UnavailableReason;

  constructor(reason: UnavailableReason, options?: { cause?: unknown }) {
    super("OPENAGENTSEARCH_UNAVAILABLE", options);
    this.name = "OpenAgentSearchUnavailable";
    this.reason = reason;
  }
}

/** Thrown when a DID does not match DID_KEY_PATTERN. Never thrown after a fetch has started. */
export class InvalidDid extends Error {
  readonly did?: string;

  constructor(did?: string) {
    super("INVALID_DID");
    this.name = "InvalidDid";
    this.did = did;
  }
}

/**
 * Thrown when a bound candidate's own identity.did disagrees with the DID it is bound to. Never
 * silently prefers one value over the other.
 */
export class BindingDidMismatch extends Error {
  readonly bindingDid?: string;
  readonly identityDid?: string;

  constructor(bindingDid?: string, identityDid?: string) {
    super("BINDING_DID_MISMATCH");
    this.name = "BindingDidMismatch";
    this.bindingDid = bindingDid;
    this.identityDid = identityDid;
  }
}
