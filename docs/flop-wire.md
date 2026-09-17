# FLOP v1 wire-format decoders

`openagentsearch.flop.wire` is a pure, fail-closed decode-and-recompute library for the public FLOP
v1 wire objects described in Appendix F of the FLOP yellow paper: `DataRef`, `DecodePolicy`,
`VerifiedTurn` (leaf versions V0-V3), the FCC4 DA transcript container, the agent receipt (v1 and
the legacy 96-byte form), and `ValidatorAttestation`. It lets a router *read and check* what the
chain and SDKs emit -- it does not talk to a chain, a wallet, or the network, and it makes no claim
of conformance beyond the public corpus it is tested against.

## Corpus pin

Every decoder, hash, and preimage in this package was verified byte-for-byte against the vendored
corpus at `tests/fixtures/flop/wire-format-v1.json`:

- Corpus commit: `cb3cbf97a346ff85aca6dba5e924434270ca672c`
- sha256: `80d4a7e70f984342eb474ae5285a17a6b9348eca887e1689b15e641922051d93`
- License: CC BY 4.0 (see `tests/fixtures/flop/NOTICE.txt` for full attribution)

`tests/test_flop_wire_corpus.py` re-hashes the vendored file on every run and fails loudly if it
has changed out from under the pin above.

## Reason vocabulary

Every decode failure in this package is a `openagentsearch.flop.wire.scale.WireDecodeError` with a
`reason` from this exact, closed set (`str(exc)` always starts with `reason`):

| Reason | Raised by | Meaning |
|---|---|---|
| `truncated` | `scale.Reader`, `objects.*` | fewer bytes remain than the field/mode needs |
| `overlong` | `scale.Reader.compact_u32` | the `Compact<u32>` value would fit in a smaller mode |
| `exceeds u32` | `scale.Reader.compact_u32` | mode-3 declared payload length > 4 bytes |
| `trailing bytes` | `scale.Reader.finish` | bytes remain after a full object was decoded |
| `invalid bool` | `scale.Reader.bool_`, FCC4 flag bytes | byte is not `0x00`/`0x01` |
| `unknown tag` | `objects.decode_leaf_version`, `decode_retention_class`, `decode_tee_type`, `DecodePolicyClass.decode_from`, `OutputTransform.decode_from` | a SCALE enum tag outside its known set |
| `unknown magic` | `objects.Fcc4Transcript.decode`, `objects.decode_receipt_with_signature` | wrong 4-byte magic / wrong receipt domain or version byte |
| `PathTooLong` | `objects.VerifiedTurn.decode_from`, `settle.verified_work_from_turns` | `merkle_path` longer than 64 entries |
| `LeafFieldsInconsistent` | `objects.VerifiedTurn.decode_from` | `leaf_version` and the zero/non-zero pattern of `decode_policy_hash`/`h_ids`/`toploc_commitment_hash` disagree (see §1.7 of the design brief) |
| `FccFieldsInconsistent` | `objects._decode_fcc4_turn` | an FCC4 per-turn record's `leaf_version`/`has_policy`/`h_ids`/`toploc_hash` pattern is inconsistent (or `leaf_version` is outside 0-3 -- FCC4's `leaf_version` is a plain `u8`, not a rejecting SCALE enum tag, so an out-of-range value falls through to this reason instead of `unknown tag`) |
| `UnsupportedLeafVersion` | `settle.verified_work_from_turns` | `channel_has_decode_policy=True` and the turn is V0/V1 -- channel STATE, not a wire-bytes violation |
| `DuplicateVerifiedTurn` | `settle.verified_work_from_turns` | the same `turn_index` appears twice in one call |
| `TurnIndexOutOfRange` | *(reserved; not raised by anything in this package)* | no corpus vector and no brief rule ties a concrete check to this reason -- it is part of the closed vocabulary but currently unused |
| `LeafNotInRoot` | `settle.verified_work_from_turns` | the recomputed leaf hash, walked up `merkle_path`, does not equal `final_root` |
| `AggregateGnOverflow` | `settle.verified_work_from_turns` | the running u128 sum of `g_n` across turns exceeds `2**128 - 1` |
| `TooManyTurns` | `settle.verified_work_from_turns` | `len(turns) > max_turns` (default 1024) |
| `BadReceiptSignature` | `verify.check_receipt_signature` (via `SignatureCheck.reason`) | an injected verifier rejected the receipt signature |
| `BadValidatorSignature` | `verify.check_validator_signature` (via `SignatureCheck.reason`) | an injected verifier rejected the validator attestation signature |
| `BadLeafSignature` | `verify.check_leaf_signature` (via `SignatureCheck.reason`) | an injected verifier rejected the leaf (`enclave_sig`) signature |

## Hash table: which object uses which algorithm

Both algorithms are corpus-proven (every value below was independently recomputed with Python's
stdlib `hashlib` and matched the corpus's own `hash_hex`/`sha256_hex` exactly):

| Object | Algorithm | Function |
|---|---|---|
| `channel_id` v1 | `blake2_256` (= `hashlib.blake2b(data, digest_size=32)`) | `hashes.channel_id_v1` |
| `task_hash` v1 | `blake2_256` | `hashes.task_hash_v1` |
| Leaf hashes V0-V3 | `blake2_256` | `hashes.leaf_hash` |
| Merkle node / root | `blake2_256` | `hashes.merkle_node`, `merkle_root_from_path`, `merkle_root_of_leaves` |
| `decode_policy_hash` v1 | `sha256` | `hashes.decode_policy_hash_v1` |
| `report_data` v1 | `sha256` (result zero-padded 32 -> 64 B) | `hashes.report_data_v1` |

`blake2s` is never used anywhere in this package -- it is a different algorithm from
`blake2b(digest_size=32)` with different output, and no corpus vector exercises it. The agent
receipt (v1 and legacy) is signed directly over its raw message bytes -- it is never hashed first.

## What is NOT verified

- **sr25519 signatures.** No sr25519 implementation exists in the Python standard library (no
  `pynacl`, `substrate-interface`, or `py-sr25519-bindings` is a dependency of this project).
  `openagentsearch.flop.wire.verify.NoVerifier` (the default when no verifier is injected) makes
  every `check_receipt_signature` / `check_leaf_signature` / `check_validator_signature` call
  report `status="not_verified"` -- never `"accepted"`. Structural checks (key/signature length,
  correct message construction) run for real regardless. To get a genuine accept/reject answer,
  inject an object satisfying `verify.SignatureVerifier` (`def verify(self, public_key: bytes,
  message: bytes, signature: bytes) -> bool`) backed by a real sr25519 implementation.
- **Channel state.** `channel_has_decode_policy` is an explicit input to
  `settle.verified_work_from_turns`, never looked up -- this package has no chain connection and no
  notion of "the current state of channel X." The caller is trusted for it, the same way
  `openagentsearch.flop.chain.ingest_finalized_facts` trusts its `ChainSource`'s `finalized_head()`.
- **The F.4 audit-challenge hash** (`blake2(audit_id || shard)`, yellow paper line 1546) has zero
  vectors anywhere in the corpus (`grep`-confirmed: no `audit`/`challenge` key exists in
  `wire-format-v1.json`) and is not implemented in this package at all.
- **`Leased` retention** for `DataRef` has no positive corpus vector -- only `Ephemeral=0` is
  exercised by `data_ref_v1`. `RetentionClass.LEASED` is implemented and round-trips
  (`test_data_ref_v1_leased_round_trips_uncovered_by_corpus`), but that round-trip is this
  package's own construction, not a corpus-verified vector.
- **No claim of conformance beyond the public corpus.** A real FLOP deployment may extend or change
  these wire formats in ways a fixed, offline corpus snapshot cannot reveal.

## Session offers (unpublished)

`openagentsearch.flop.offer` is a *separate* seam from the wire-format decoders above: it exists
for the `SessionOffer` opening object (runtime `StandingOffer` / SDK `SessionOffer`, v1 spot and
v2 forward), not for the `DataRef`/`VerifiedTurn`/receipt/attestation objects this document covers.

FLOP maintainer `sv` stated on `flop-labs/yellowpaper#26` (2026-09-14) that `SessionOffer` is
defined only in the **private** `flop-labs/flop-core` repository (commit `41d0009`); a versioned
quote/discovery contract with a signed wire shape is planned for a future appendix, but the
published yellow paper v0.5.0 does not contain it, and `flop-labs/flop-core` answers 404 to the
public (verified 2026-09-17).

Until that shape is published, `parse_session_offer(data: bytes | bytearray, *, max_bytes=65536)
-> OfferParseResult` never inspects `data`'s content -- it validates the type and bound of the input,
hashes the raw bytes (`input_sha256`), and always returns `status ==
"OFFER_SHAPE_UNPUBLISHED"`. A well-formed future `SessionOffer` and 4 KiB of random bytes get the
exact same answer today: this module has no way to distinguish them, and does not pretend to.

`offer_shape_status()` returns the single `OfferShapeStatus` describing that state:
`published=False`, a `source` naming the private commit and sv's comment, `watch` (the places a
publication will show up), and `binds` -- a tuple of twelve snake_case field names (`miner`,
`chain_genesis`, `model_hash`, `precision`, `enclave_key`, `minimum_escrow`, `sla_bounds`,
`advisory_capacity_hint`, `expiry`, `nonce`, `signature`, `forward_terms`) taken from sv's prose
description of what the object "authoritatively binds." **`binds` is vocabulary from a public
comment, not a schema**: no field types, encodings, byte ordering, or signing domain are public,
and `binds` will be replaced -- not extended -- the day the real shape lands.

A caller (the future `/route`, package D2) may conclude exactly nothing about an offer's validity
from `OFFER_SHAPE_UNPUBLISHED`: not accepted, not rejected as malformed, unknown. Treating this
status as a rejection would be wrong the same way treating it as an acceptance would be wrong --
until the shape is public, this package cannot tell the two apart.

## One documented corpus discrepancy: `wrong_path_orientation`

The corpus's `negative_cases` entry `wrong_path_orientation` states `"expected": "reject
LeafNotInRoot"` for a mutation that flips the `sibling_is_left` flag of the first Merkle-path step
of the V3 example turn. Byte-level analysis (not assumption) shows this specific step's sibling is
a self-duplicate of the leaf's own hash -- `compute_channel_v1`'s 3-leaf tree duplicates its last
(odd-one-out) leaf against itself, and leaf index 2 (the V3 example) is that leaf. Combining two
identical 32-byte values (`blake2_256(X || X)`) is byte-for-byte the same string regardless of
which copy is called "left," so no implementation of the documented Merkle-walk algorithm --
correct or not -- can distinguish the flipped flag from the original at that specific step.
`tests/test_flop_wire_corpus.py::test_negative_case_wrong_path_orientation_root_is_unaffected`
proves this against the real corpus bytes and documents it in place of asserting the corpus's
stated (and, for this vector, unreachable) `LeafNotInRoot` outcome. The same defect was reported
upstream as flop-labs/yellowpaper issue #44 (2026-09-11), with a reproduction from the corpus
generator and two candidate fixes (flip a non-self-duplicate path item, or state an explicit
orientation rule in F.3); two further independent implementations confirmed it there. This
repository does not file a duplicate; the test above will start failing the day the corpus
vector is regenerated at a position where orientation is observable, which is the intended signal.
