"""Pure settlement arithmetic (R11.2a): given already-decoded `VerifiedTurn`s and channel-state
facts the CALLER supplies (this module never looks anything up), recompute each turn's leaf hash,
walk its Merkle path against the channel's `final_root`, sum `g_n` with overflow checking, and
report per-turn signature checks. No chain state, no persistence, no network -- `channel_id`,
`final_root` and `channel_has_decode_policy` are explicit inputs, not read from anywhere.

NOT guaranteed: this module does not know whether `channel_has_decode_policy` or `final_root` are
themselves true of any real channel -- it trusts the caller for both, exactly as
`openagentsearch.flop.chain.ingest_finalized_facts` trusts `finalized_head()` (see that module's
docstring for the same pattern). A `SettlementCheck` is a report of what the given bytes and inputs
imply, not a settlement -- it performs no writes and calls no chain.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from .hashes import merkle_root_from_path
from .objects import LeafVersion, VerifiedTurn
from .scale import WireDecodeError
from .verify import SignatureCheck, SignatureVerifier, check_leaf_signature

_U128_MAX = 2**128 - 1
_DEFAULT_MAX_TURNS = 1024
_DEFAULT_MAX_PATH_LEN = 64


def _require_h256(name: str, value: bytes) -> None:
    if not isinstance(value, bytes) or len(value) != 32:
        raise ValueError(f"{name} must be exactly 32 bytes (H256), got {value!r}")


@dataclass(frozen=True)
class SettlementCheck:
    """The outcome of one `verified_work_from_turns` run: `turn_count` turns were all found
    in-root, non-duplicate, and within the aggregate `g_n` bound; `leaf_hashes[i]` /
    `signatures[i]` correspond to `turns[i]` from the call that produced this (same order, same
    length)."""

    turn_count: int
    aggregate_gn: int
    leaf_hashes: tuple[bytes, ...]
    signatures: tuple[SignatureCheck, ...]

    def __post_init__(self) -> None:
        bad_turn_count = (
            not isinstance(self.turn_count, int)
            or isinstance(self.turn_count, bool)
            or self.turn_count < 0
        )
        if bad_turn_count:
            raise ValueError(f"turn_count must be a non-negative int, got {self.turn_count!r}")
        if (
            not isinstance(self.aggregate_gn, int)
            or isinstance(self.aggregate_gn, bool)
            or not (0 <= self.aggregate_gn <= _U128_MAX)
        ):
            raise ValueError(f"aggregate_gn must be in [0, 2**128-1], got {self.aggregate_gn!r}")
        if len(self.leaf_hashes) != self.turn_count or len(self.signatures) != self.turn_count:
            raise ValueError(
                "leaf_hashes and signatures must each have turn_count entries, got "
                f"{len(self.leaf_hashes)} and {len(self.signatures)} "
                f"for turn_count={self.turn_count}"
            )
        for index, leaf_hash in enumerate(self.leaf_hashes):
            _require_h256(f"leaf_hashes[{index}]", leaf_hash)


def verified_work_from_turns(
    turns: Sequence[VerifiedTurn],
    *,
    channel_id: bytes,
    final_root: bytes,
    channel_has_decode_policy: bool,
    enclave_public_key: bytes | None = None,
    verifier: SignatureVerifier | None = None,
    max_turns: int = _DEFAULT_MAX_TURNS,
    max_path_len: int = _DEFAULT_MAX_PATH_LEN,
) -> SettlementCheck:
    """Check `turns` against `channel_id`/`final_root`, in this exact order:

    1. `len(turns) > max_turns` -> `WireDecodeError("TooManyTurns", ...)`.
    2. Per turn, in order:
       a. `channel_has_decode_policy and turn.leaf_version in (V0, V1)` ->
          `WireDecodeError("UnsupportedLeafVersion", ...)` -- a channel that has pinned a decode
          policy no longer accepts the pre-policy leaf versions, even though those versions decode
          structurally cleanly on their own (this is channel STATE, supplied by the caller, not a
          wire-bytes check).
       b. `len(turn.merkle_path) > max_path_len` -> `WireDecodeError("PathTooLong", ...)`.
       c. Recompute the leaf hash (`turn.leaf_hash(channel_id)`) and walk `turn.merkle_path`
          (`merkle_root_from_path`); if the result != `final_root` ->
          `WireDecodeError("LeafNotInRoot", ...)`.
       d. `turn.turn_index` already seen in this call ->
          `WireDecodeError("DuplicateVerifiedTurn", ...)`.
       e. Add `turn.g_n` to the running u128 total; if it now exceeds `2**128 - 1` ->
          `WireDecodeError("AggregateGnOverflow", ...)`.
    3. When `enclave_public_key` is given, each turn's `enclave_sig` is checked against its
       recomputed leaf hash through `verifier` (`check_leaf_signature`); without
       `enclave_public_key`, every `SignatureCheck` in the result is `"not_verified"` and no
       signature is even structurally checked (there is no key to check it against).

    Raises `WireDecodeError` with the reason on the first violation, in the order above -- never
    returns a partial `SettlementCheck` for a rejected batch."""
    _require_h256("channel_id", channel_id)
    _require_h256("final_root", final_root)
    if len(turns) > max_turns:
        raise WireDecodeError(
            "TooManyTurns", len(turns), f"{len(turns)} turns > max_turns={max_turns}"
        )

    seen_turn_indices: set[int] = set()
    leaf_hashes: list[bytes] = []
    signatures: list[SignatureCheck] = []
    aggregate_gn = 0

    for position, turn in enumerate(turns):
        if channel_has_decode_policy and turn.leaf_version in (LeafVersion.V0, LeafVersion.V1):
            raise WireDecodeError(
                "UnsupportedLeafVersion",
                position,
                f"leaf_version={turn.leaf_version.name} not accepted once a decode "
                "policy is pinned",
            )
        if len(turn.merkle_path) > max_path_len:
            raise WireDecodeError(
                "PathTooLong",
                position,
                f"merkle_path length {len(turn.merkle_path)} > max_path_len={max_path_len}",
            )

        leaf_hash = turn.leaf_hash(channel_id)
        recomputed_root = merkle_root_from_path(leaf_hash, turn.merkle_path)
        if recomputed_root != final_root:
            raise WireDecodeError(
                "LeafNotInRoot",
                position,
                f"turn_index={turn.turn_index} not found under final_root",
            )

        if turn.turn_index in seen_turn_indices:
            raise WireDecodeError(
                "DuplicateVerifiedTurn", position, f"turn_index={turn.turn_index} repeated"
            )
        seen_turn_indices.add(turn.turn_index)

        aggregate_gn += turn.g_n
        if aggregate_gn > _U128_MAX:
            raise WireDecodeError(
                "AggregateGnOverflow",
                position,
                f"aggregate g_n exceeds 2**128-1 at turn {position}",
            )

        leaf_hashes.append(leaf_hash)
        if enclave_public_key is not None:
            signatures.append(
                check_leaf_signature(leaf_hash, enclave_public_key, turn.enclave_sig, verifier)
            )
        else:
            signatures.append(
                SignatureCheck(
                    status="not_verified",
                    reason="no sr25519 verifier available",
                    message_len=len(leaf_hash),
                )
            )

    return SettlementCheck(
        turn_count=len(turns),
        aggregate_gn=aggregate_gn,
        leaf_hashes=tuple(leaf_hashes),
        signatures=tuple(signatures),
    )
