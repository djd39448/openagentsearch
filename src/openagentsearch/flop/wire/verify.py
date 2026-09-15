"""The signature seam: structural signature checks (key/signature length, message construction)
run unconditionally and for real; the actual sr25519 cryptographic check runs ONLY through an
injected `SignatureVerifier`, because no sr25519 implementation exists in the Python standard
library (no `pynacl`, no `substrate-interface`, no `py-sr25519-bindings` -- see
`docs/flop-wire.md`).

This module never fakes a pass: without a real verifier, every check reports `"not_verified"`,
never `"accepted"`. It is the caller's job to decide what `"not_verified"` means for their use case
(refuse to act on it, log it, queue it for a real verifier elsewhere) -- this module does not
decide that for them.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from .objects import ValidatorAttestation


def _require_bytes_len(name: str, value: bytes, length: int) -> None:
    if not isinstance(value, bytes) or len(value) != length:
        raise ValueError(f"{name} must be exactly {length} bytes, got {value!r}")


class _HasMessage(Protocol):
    """Structural shape `check_receipt_signature` needs -- satisfied by both `objects.ReceiptV1`
    and `objects.LegacyReceipt` without this function needing to accept a union of the two."""

    def message(self) -> bytes: ...


class SignatureVerifier(Protocol):
    """A real sr25519 verifier, injected by the caller. The scheme is sr25519 over the raw message
    bytes (Appendix F.0): no hash-first step for the receipt message, a 32-byte leaf hash for the
    leaf signature, and the 179-byte signable subset for the validator attestation."""

    def verify(self, public_key: bytes, message: bytes, signature: bytes) -> bool:
        """Return `True` iff `signature` is a valid sr25519 signature by `public_key` over
        `message`. Implementations should raise on malformed `public_key`/`signature` rather than
        silently returning `False`, but this module does not depend on that -- it validates length
        itself before ever calling `verify`."""
        ...


class NoVerifier:
    """The default: no sr25519 implementation exists in the Python standard library, so every
    signature check made with `NoVerifier` (or with `verifier=None`, which this module treats
    identically) reports `status="not_verified"`. `NoVerifier().verify(...)` is never actually
    called by this module's `check_*` functions -- they special-case `NoVerifier` before reaching a
    call -- but it is provided as an explicit, named, self-documenting stand-in for "no verifier",
    for callers who want to pass something rather than `None`."""

    def verify(self, public_key: bytes, message: bytes, signature: bytes) -> bool:
        raise NotImplementedError(
            "no sr25519 implementation available in this package; inject a real SignatureVerifier"
        )


class StubVerifier:
    """Test-only verifier: returns `True` only for the exact `(public_key, message, signature)`
    triples it was constructed with, `False` for everything else. Never returns `True` by
    surprise -- an empty `StubVerifier(())` rejects every check."""

    def __init__(self, valid: Iterable[tuple[bytes, bytes, bytes]]) -> None:
        self._valid = frozenset(valid)

    def verify(self, public_key: bytes, message: bytes, signature: bytes) -> bool:
        return (public_key, message, signature) in self._valid


@dataclass(frozen=True)
class SignatureCheck:
    """The outcome of one signature check. `status` is `"accepted"` (a real verifier confirmed the
    signature), `"rejected"` (a real verifier confirmed it does NOT match), or `"not_verified"` (no
    real verifier was available -- this is NOT the same as `"accepted"` and must never be treated
    as one). `reason` is `""` for `"accepted"`, one of `BadReceiptSignature` /
    `BadValidatorSignature` / `BadLeafSignature` for `"rejected"`, or
    `"no sr25519 verifier available"` for `"not_verified"`."""

    status: str
    reason: str
    message_len: int
    scheme: str = "sr25519"

    def __post_init__(self) -> None:
        if self.status not in ("accepted", "rejected", "not_verified"):
            raise ValueError(f"status must be accepted/rejected/not_verified, got {self.status!r}")
        if not isinstance(self.reason, str):
            raise ValueError(f"reason must be a str, got {self.reason!r}")
        if not isinstance(self.message_len, int) or isinstance(self.message_len, bool):
            raise ValueError(f"message_len must be an int, got {self.message_len!r}")
        if self.message_len < 0:
            raise ValueError(f"message_len must be non-negative, got {self.message_len!r}")


def _run(
    verifier: SignatureVerifier | None, public_key: bytes, message: bytes, signature: bytes
) -> bool | None:
    """`None` means "no real verifier available" (`verifier is None` or a `NoVerifier` instance);
    otherwise the injected verifier's own `True`/`False` result."""
    if verifier is None or isinstance(verifier, NoVerifier):
        return None
    return verifier.verify(public_key, message, signature)


def _check(
    verifier: SignatureVerifier | None,
    public_key: bytes,
    message: bytes,
    signature: bytes,
    bad_reason: str,
) -> SignatureCheck:
    _require_bytes_len("public_key", public_key, 32)
    _require_bytes_len("signature", signature, 64)
    result = _run(verifier, public_key, message, signature)
    if result is None:
        return SignatureCheck(
            status="not_verified", reason="no sr25519 verifier available", message_len=len(message)
        )
    if result:
        return SignatureCheck(status="accepted", reason="", message_len=len(message))
    return SignatureCheck(status="rejected", reason=bad_reason, message_len=len(message))


def check_receipt_signature(
    receipt: _HasMessage,
    public_key: bytes,
    signature: bytes,
    verifier: SignatureVerifier | None = None,
) -> SignatureCheck:
    """Check `signature` over `receipt.message()` (either a `ReceiptV1` or a `LegacyReceipt` --
    anything with a zero-argument `message() -> bytes` method). Structural checks (32-byte
    `public_key`, 64-byte `signature`) always run and raise `ValueError` on misuse; the
    cryptographic check runs only through `verifier`."""
    message = receipt.message()
    return _check(verifier, public_key, message, signature, "BadReceiptSignature")


def check_leaf_signature(
    leaf_hash: bytes,
    public_key: bytes,
    signature: bytes,
    verifier: SignatureVerifier | None = None,
) -> SignatureCheck:
    """Check `signature` over the 32-byte `leaf_hash` directly (not a raw preimage -- the leaf
    signature signs the hash, per F.3). Structural checks always run; the cryptographic check runs
    only through `verifier`."""
    _require_bytes_len("leaf_hash", leaf_hash, 32)
    return _check(verifier, public_key, leaf_hash, signature, "BadLeafSignature")


def check_validator_signature(
    attestation: ValidatorAttestation, verifier: SignatureVerifier | None = None
) -> SignatureCheck:
    """Check `attestation.signature` over `attestation.signable_bytes()`, using
    `attestation.validator_id` as the public key. Structural checks always run; the cryptographic
    check runs only through `verifier`."""
    return _check(
        verifier,
        attestation.validator_id,
        attestation.signable_bytes(),
        attestation.signature,
        "BadValidatorSignature",
    )
