"""Package D4a: generates a deterministic, committed conformance report for the FLOP v1 wire
decoders (`openagentsearch.flop.wire`) against the vendored public corpus
`tests/fixtures/flop/wire-format-v1.json`.

`retardio73-boop/flop-conformance-lab` (their #58 checklist) only retains downstream
reproductions that ship the exact script, the full output, the pinned upstream commit, sha256s
and the regenerate command. `tests/test_flop_wire_corpus.py` already exercises the whole public
corpus; this script performs EXACTLY those same checks (read that file first -- this module
mirrors its logic case for case, never weakening one) and serializes the result as one
byte-deterministic JSON document, so a third party can diff their own run against
`tests/fixtures/flop/wire-format-v1-report.json` without re-deriving anything.

Usage: python scripts/make_wire_conformance_report.py [--corpus PATH] [--out PATH]

`--corpus` defaults to `tests/fixtures/flop/wire-format-v1.json` (relative to the current working
directory, matching how the test suite is run). `--out` defaults to stdout. Exit 0 whenever the
corpus parses as JSON -- mismatches, non-rejections and not-verifiable signature checks are all
reported INSIDE the JSON document, never as a nonzero exit code; the report is the evidence, not
the exit code. Exit 2 only for a bad argument or an unreadable/unparsable corpus file.

Determinism: the output contains no timestamps, no absolute paths and no environment data --
`sort_keys=True`, `indent=2`, `ensure_ascii=False`, a single trailing LF, decoded and re-encoded
as UTF-8 regardless of platform.

Negative-case comparison rule (mirrors what `tests/test_flop_wire_corpus.py` actually checks, not
just the corpus's own prose): a corpus `negative_cases[*].expected` value takes one of three
shapes, and each is matched differently --

1. `"reject"` (bare, no reason named): the test only asserts *some* `WireDecodeError` is raised
   (the specific reason is a separate, hand-coded expectation the test file carries, not the
   corpus's own text) -- so here, `matches_expected` is `True` iff the decoder rejects for ANY
   reason.
2. `"reject <ReasonName>"`: the test asserts `exc.reason == "<ReasonName>"` exactly -- so here,
   `matches_expected` is `True` iff the decoder rejects with that exact reason string (whether the
   rejection is a `WireDecodeError.reason` or a `verify.SignatureCheck.reason`).
3. Anything not starting with `"reject"` (currently only `"different channel_id"`, for
   `wrong_genesis_network`/`wrong_session`): these are not decode rejections at all -- the test
   recomputes `channel_id_v1` with the mutated field substituted and asserts the result differs
   from the baseline. Here, `matches_expected` is `True` iff that recomputation actually differs.

Two further rules, both stated in the package spec (`handoff/D4a-SPEC.md`) rather than inferred:
`invalid_receipt_signature`, `invalid_validator_signature` and `legacy_receipt_current_channel`
(every case whose expected outcome is a signature verdict) are always reported `not_verifiable`
with `matches_expected: null` -- this package has no sr25519 implementation, and the corpus test's
own "acceptance"/"rejection" for these two ids is produced by an injected `StubVerifier` (a test
device), which this report never treats as evidence of a real cryptographic result.
`wrong_path_orientation` is a known, upstream-reported corpus defect (see `docs/flop-wire.md`'s
"One documented corpus discrepancy" section and `flop-labs/yellowpaper#44`): the mutated
Merkle-path step's sibling is a byte-for-byte self-duplicate of the leaf's own hash, so no
implementation of the documented walk can detect the flip at that step. When (and only when) this
script's own live recomputation reproduces that non-detection, the case is reported
`not_rejected`, `matches_expected: false`, and mirrored into the top-level `deviations` list.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))  # scripts/ is not a package

from openagentsearch.flop.wire import hashes, objects, settle, verify  # noqa: E402
from openagentsearch.flop.wire.scale import (  # noqa: E402
    Reader,
    WireDecodeError,
    decode_compact_u32,
    encode_compact_u32,
)

Case = dict[str, Any]
Corpus = dict[str, Any]

DEFAULT_CORPUS = "tests/fixtures/flop/wire-format-v1.json"

_CORPUS_REPOSITORY = "flop-labs/yellowpaper"
_CORPUS_COMMIT = "cb3cbf97a346ff85aca6dba5e924434270ca672c"
_CORPUS_REL_PATH = "evidence/wire-format-v1.json"
_IMPL_REPOSITORY = "djd39448/openagentsearch"
_IMPL_MODULE = "openagentsearch.flop.wire"
_REGENERATE_CMD = (
    "python scripts/make_wire_conformance_report.py "
    "--corpus tests/fixtures/flop/wire-format-v1.json "
    "--out tests/fixtures/flop/wire-format-v1-report.json"
)
_UPSTREAM_ISSUE = "https://github.com/flop-labs/yellowpaper/issues/44"

_MALFORMED_COMPACT_REASON_MAP = {
    "truncated": "truncated",
    "truncated mode 1": "truncated",
    "truncated mode 2": "truncated",
    "overlong zero": "overlong",
    "overlong big mode": "overlong",
    "exceeds u32": "exceeds u32",
}
_EXPECTED_LEAF_PREIMAGE_LEN = {"V0": 116, "V1": 140, "V2": 172, "V3": 236}


def h(hex_str: str) -> bytes:
    """`bytes.fromhex(hex_str)` -- the same shorthand `tests/test_flop_wire_corpus.py` uses,
    given a concrete `bytes` return type so downstream comparisons against corpus-derived `Any`
    values are never themselves inferred `Any`."""
    return bytes.fromhex(hex_str)


def _slug(text: str) -> str:
    return text.lower().replace(" ", "_").replace("/", "_")


def _reject_match(expected: str, actual_reason: str) -> bool:
    """See the module docstring's "Negative-case comparison rule", forms 1 and 2."""
    if not expected.startswith("reject"):
        return False
    tail = expected[len("reject") :].strip()
    if not tail:
        return True
    return tail == actual_reason


# ================================================================================================
# Positive vectors
# ================================================================================================


def _vector(vector_id: str, checks: dict[str, bool], detail_ok: str) -> dict[str, object]:
    failed = [name for name, ok in checks.items() if not ok]
    result = "ok" if not failed else "mismatch"
    detail = detail_ok if not failed else f"failed: {', '.join(failed)}"
    return {"id": vector_id, "checks": list(checks.keys()), "result": result, "detail": detail}


def _try_vector(
    vector_id: str, builder: Callable[[], dict[str, bool]], detail_ok: str
) -> dict[str, object]:
    """Runs `builder` (one vector's worth of boolean checks, mirroring one `test_*` function or
    one parametrized case) and never lets it crash the report: any unexpected exception (a bug,
    or an environment/corpus mismatch this script did not anticipate) becomes a `"mismatch"`
    vector with the exception recorded in `detail`, not a process failure."""
    try:
        checks = builder()
    except Exception as exc:  # the report must never crash on a corpus/environment mismatch
        return {
            "id": vector_id,
            "checks": [],
            "result": "mismatch",
            "detail": f"raised {type(exc).__name__}: {exc}",
        }
    return _vector(vector_id, checks, detail_ok)


class _LeafInputsTurn:
    """Minimal stand-in satisfying `hashes.LeafFields`, built from `compute_channel_v1.leaf_inputs`
    -- the same trick `tests/test_flop_wire_corpus.py::_Turn` uses to exercise
    `hashes.leaf_preimage`/`leaf_hash` directly, independent of `objects.VerifiedTurn`."""

    def __init__(self, leaf_inputs: Case) -> None:
        self.turn_index = int(leaf_inputs["turn_index"])
        self.h_in = h(leaf_inputs["h_in_hex"])
        self.h_out = h(leaf_inputs["h_out_hex"])
        self.g_n = int(leaf_inputs["g_n"])
        self.decode_policy_hash = h(leaf_inputs["decode_policy_hash_hex"])
        self.h_ids = h(leaf_inputs["h_ids_hex"])
        self.toploc_commitment_hash = h(leaf_inputs["toploc_commitment_hash_hex"])
        self.miner_recv_ms = int(leaf_inputs["miner_recv_ms"])
        self.miner_done_ms = int(leaf_inputs["miner_done_ms"])
        self.latency_ms = int(leaf_inputs["latency_ms"])


def _vectors_codec(corpus: Corpus) -> list[dict[str, object]]:
    vectors: list[dict[str, object]] = []

    for case in corpus["codec"]["malformed_compact"]:
        expected_reason = _MALFORMED_COMPACT_REASON_MAP[str(case["reason"])]
        vector_id = f"codec.malformed_compact.{_slug(str(case['reason']))}"

        def build(case: Case = case, expected_reason: str = expected_reason) -> dict[str, bool]:
            try:
                decode_compact_u32(h(case["bytes_hex"]))
            except WireDecodeError as exc:
                return {
                    "raises_WireDecodeError": True,
                    "reason_matches": exc.reason == expected_reason,
                    "str_starts_with_reason": str(exc).startswith(expected_reason),
                }
            return {
                "raises_WireDecodeError": False,
                "reason_matches": False,
                "str_starts_with_reason": False,
            }

        vectors.append(_try_vector(vector_id, build, f"rejected as {expected_reason!r}"))

    for case in corpus["codec"]["scale_compact_u32"]:
        vector_id = f"codec.scale_compact_u32.{case['value']}"

        def build_compact_u32(case: Case = case) -> dict[str, bool]:
            value = decode_compact_u32(h(case["bytes_hex"]))
            return {
                "value_matches": value == int(case["value"]),
                "round_trip_matches": encode_compact_u32(value) == h(case["bytes_hex"]),
            }

        vectors.append(
            _try_vector(vector_id, build_compact_u32, "decoded value and round-trip match")
        )

    def build_scale_bool() -> dict[str, bool]:
        sb = corpus["codec"]["scale_bool"]
        other_rejected = False
        other_reason_ok = False
        try:
            Reader(bytes([0x02])).bool_()
        except WireDecodeError as exc:
            other_rejected = True
            other_reason_ok = exc.reason == "invalid bool"
        return {
            "false_decodes_false": Reader(h(sb["false"])).bool_() is False,
            "true_decodes_true": Reader(h(sb["true"])).bool_() is True,
            "other_is_reject_in_corpus": str(sb["other"]) == "reject",
            "other_byte_rejected": other_rejected,
            "other_reason_is_invalid_bool": other_reason_ok,
        }

    vectors.append(_try_vector("codec.scale_bool", build_scale_bool, "0x00/0x01/other all agree"))

    def build_fixed_integers() -> dict[str, bool]:
        description = str(corpus["codec"]["fixed_integers"])
        return {"little_endian_mentioned": "little-endian" in description}

    vectors.append(
        _try_vector("codec.fixed_integers", build_fixed_integers, "description present")
    )
    return vectors


def _vectors_compute_channel(corpus: Corpus) -> list[dict[str, object]]:
    vectors: list[dict[str, object]] = []
    cc = corpus["compute_channel_v1"]

    def build_channel_id() -> dict[str, bool]:
        ci = cc["channel_id"]
        inputs = ci["inputs"]
        preimage = hashes.channel_id_v1_preimage(
            h(inputs["genesis_hash_hex"]),
            h(inputs["agent_account_id32_hex"]),
            h(inputs["miner_account_id32_hex"]),
            int(inputs["nonce"]),
        )
        digest = hashes.channel_id_v1(
            h(inputs["genesis_hash_hex"]),
            h(inputs["agent_account_id32_hex"]),
            h(inputs["miner_account_id32_hex"]),
            int(inputs["nonce"]),
        )
        return {
            "preimage_bytes": preimage == h(ci["preimage_hex"]),
            "digest": digest == h(ci["hash_hex"]),
        }

    vectors.append(
        _try_vector("compute_channel_v1.channel_id", build_channel_id, "preimage and digest match")
    )

    def build_domains() -> dict[str, bool]:
        domains = cc["domains"]
        return {
            "channel_id_ascii": str(domains["channel_id_ascii"]) == "FLOP/COMPUTE_CHANNEL/ID",
            "receipt_ascii": str(domains["receipt_ascii"]) == "FLOP/COMPUTE_CHANNEL/RECEIPT",
        }

    vectors.append(_try_vector("compute_channel_v1.domains", build_domains, "ascii strings match"))

    for entry in cc["leaf_versions"]:
        vector_id = f"compute_channel_v1.leaf_versions.{entry['version']}"

        def build(entry: Case = entry) -> dict[str, bool]:
            turn = _LeafInputsTurn(cc["leaf_inputs"])
            channel_id = h(cc["leaf_inputs"]["channel_id_hex"])
            version = int(entry["scale_tag"])
            preimage = hashes.leaf_preimage(version, channel_id, turn)
            expected_len = _EXPECTED_LEAF_PREIMAGE_LEN[str(entry["version"])]
            return {
                "length": len(preimage) == expected_len,
                "preimage_bytes": preimage == h(entry["preimage_hex"]),
                "hash": hashes.leaf_hash(version, channel_id, turn) == h(entry["hash_hex"]),
            }

        vectors.append(_try_vector(vector_id, build, "length/preimage/hash match"))

    def build_merkle_path() -> dict[str, bool]:
        mk = cc["merkle"]
        v3_hash = h(cc["leaf_versions"][3]["hash_hex"])
        path = [
            (h(step["sibling_hex"]), bool(step["sibling_is_left"]))
            for step in mk["path_for_index_2"]
        ]
        root = hashes.merkle_root_from_path(v3_hash, path)
        return {"root_matches": root == h(mk["root_hex"])}

    vectors.append(
        _try_vector(
            "compute_channel_v1.merkle.path_for_index_2", build_merkle_path, "root matches"
        )
    )

    def build_merkle_leaves() -> dict[str, bool]:
        mk = cc["merkle"]
        by_version = {e["version"]: h(e["hash_hex"]) for e in cc["leaf_versions"]}
        leaves = [by_version[version] for version in mk["leaf_order"]]
        return {
            "odd_node_behavior_is_duplicate_last": str(mk["odd_node_behavior"]) == "duplicate last",
            "root_matches": hashes.merkle_root_of_leaves(leaves) == h(mk["root_hex"]),
        }

    vectors.append(
        _try_vector(
            "compute_channel_v1.merkle.root_of_leaves", build_merkle_leaves, "root matches"
        )
    )

    def build_receipt_message() -> dict[str, bool]:
        rc = cc["receipt"]
        receipt = objects.ReceiptV1(
            channel_id=h(rc["inputs"]["channel_id_hex"]),
            final_root=h(rc["inputs"]["final_root_hex"]),
            aggregate_gn=int(rc["inputs"]["aggregate_gn"]),
            payable=int(rc["inputs"]["payable"]),
        )
        fn_result = hashes.receipt_message_v1(
            receipt.channel_id, receipt.final_root, receipt.aggregate_gn, receipt.payable
        )
        return {
            "message_matches_preimage": receipt.message() == h(rc["preimage_hex"]),
            "hashes_fn_matches_preimage": fn_result == h(rc["preimage_hex"]),
        }

    vectors.append(
        _try_vector("compute_channel_v1.receipt.message", build_receipt_message, "messages match")
    )

    def build_receipt_decode() -> dict[str, bool]:
        rc = cc["receipt"]
        blob = h(rc["preimage_hex"]) + h(rc["signature_hex"])
        decoded, signature = objects.decode_receipt_with_signature(blob)
        is_v1 = isinstance(decoded, objects.ReceiptV1)
        return {
            "decodes_as_ReceiptV1": is_v1,
            "message_matches": is_v1 and decoded.message() == h(rc["preimage_hex"]),
            "signature_matches": signature == h(rc["signature_hex"]),
        }

    vectors.append(
        _try_vector(
            "compute_channel_v1.receipt.decode_with_signature",
            build_receipt_decode,
            "decodes and round-trips",
        )
    )

    def build_verified_turn_v3() -> dict[str, bool]:
        turn = objects.VerifiedTurn.decode(h(cc["verified_turn_v3_scale_hex"]))
        li = cc["leaf_inputs"]
        fields_match = (
            turn.leaf_version == objects.LeafVersion.V3
            and turn.turn_index == int(li["turn_index"])
            and turn.h_in == h(li["h_in_hex"])
            and turn.h_out == h(li["h_out_hex"])
            and turn.g_n == int(li["g_n"])
            and turn.decode_policy_hash == h(li["decode_policy_hash_hex"])
            and turn.h_ids == h(li["h_ids_hex"])
            and turn.toploc_commitment_hash == h(li["toploc_commitment_hash_hex"])
            and turn.miner_recv_ms == int(li["miner_recv_ms"])
            and turn.miner_done_ms == int(li["miner_done_ms"])
            and turn.latency_ms == int(li["latency_ms"])
            and turn.enclave_sig == h(cc["v3_leaf_signature"]["signature_hex"])
        )
        channel_id = h(li["channel_id_hex"])
        leaf_hash_value = turn.leaf_hash(channel_id)
        root = hashes.merkle_root_from_path(leaf_hash_value, turn.merkle_path)
        return {
            "decoded_fields_match": fields_match,
            "leaf_hash_matches": leaf_hash_value == h(cc["leaf_versions"][3]["hash_hex"]),
            "merkle_root_matches": root == h(cc["merkle"]["root_hex"]),
        }

    vectors.append(
        _try_vector(
            "compute_channel_v1.verified_turn_v3_scale_hex",
            build_verified_turn_v3,
            "decodes and proves membership",
        )
    )

    def build_v3_leaf_signature() -> dict[str, bool]:
        v3sig_hash = str(cc["v3_leaf_signature"]["leaf_hash_hex"])
        leaf_version_hash = str(cc["leaf_versions"][3]["hash_hex"])
        return {"leaf_hash_matches_leaf_versions": v3sig_hash == leaf_version_hash}

    vectors.append(
        _try_vector(
            "compute_channel_v1.v3_leaf_signature", build_v3_leaf_signature, "hashes agree"
        )
    )

    def build_fcc4() -> dict[str, bool]:
        transcript = objects.Fcc4Transcript.decode(h(cc["fcc4_transcript_blob_hex"]))
        li = cc["leaf_inputs"]
        count_ok = len(transcript.turns) == 1
        turn_ok = False
        if count_ok:
            turn = transcript.turns[0]
            turn_ok = (
                turn.leaf_version == 3
                and turn.turn_index == int(li["turn_index"])
                and turn.h_in == h(li["h_in_hex"])
                and turn.h_out == h(li["h_out_hex"])
                and turn.g_n == int(li["g_n"])
                and turn.has_policy is True
                and turn.policy_hash == h(li["decode_policy_hash_hex"])
                and turn.h_ids == h(li["h_ids_hex"])
                and turn.toploc_hash == h(li["toploc_commitment_hash_hex"])
                and turn.miner_recv_ms == int(li["miner_recv_ms"])
                and turn.miner_done_ms == int(li["miner_done_ms"])
                and turn.latency_ms == int(li["latency_ms"])
                and turn.enclave_sig == h(cc["v3_leaf_signature"]["signature_hex"])
                and turn.has_ack is False
                and turn.send_ms is None
                and turn.receive_ms is None
                and turn.agent_sig is None
            )
        return {
            "channel_id_matches": transcript.channel_id == h(li["channel_id_hex"]),
            "turn_count_is_one": count_ok,
            "turn_fields_match": turn_ok,
        }

    vectors.append(
        _try_vector(
            "compute_channel_v1.fcc4_transcript_blob_hex", build_fcc4, "decodes one V3 turn"
        )
    )
    return vectors


def _vectors_misc(corpus: Corpus) -> list[dict[str, object]]:
    vectors: list[dict[str, object]] = []

    def build_coverage() -> dict[str, bool]:
        coverage = corpus["coverage"]
        return {
            "length_is_6": len(coverage) == 6,
            "entry_shape": all(
                set(entry) == {"appendix", "consumer", "family"} for entry in coverage
            ),
        }

    vectors.append(_try_vector("coverage", build_coverage, "6 rows, consistent shape"))

    def build_data_ref() -> dict[str, bool]:
        drf = corpus["data_ref_v1"]
        decoded = objects.DataRef.decode(h(drf["scale_bytes_hex"]))
        return {
            "commitment_matches": decoded.commitment == h(drf["input"]["commitment_hex"]),
            "provider_id_matches": decoded.provider_id == int(drf["input"]["provider_id"]),
            "retention_is_ephemeral": decoded.retention == objects.RetentionClass.EPHEMERAL,
            "encode_round_trip": decoded.encode() == h(drf["scale_bytes_hex"]),
        }

    vectors.append(_try_vector("data_ref_v1", build_data_ref, "decodes and round-trips"))

    def build_data_ref_leased() -> dict[str, bool]:
        commitment = h(corpus["data_ref_v1"]["input"]["commitment_hex"])
        built = objects.DataRef(
            commitment=commitment, provider_id=5, retention=objects.RetentionClass.LEASED
        )
        round_tripped = objects.DataRef.decode(built.encode())
        return {"round_trip_equals_built": round_tripped == built}

    vectors.append(
        _try_vector(
            "data_ref_v1.leased_round_trip",
            build_data_ref_leased,
            "round-trips (not corpus-covered: this script's own constructed Leased DataRef)",
        )
    )

    def build_decode_policy() -> dict[str, bool]:
        dp = corpus["decode_policy_v1"]
        policy = objects.DecodePolicy.decode(h(dp["scale_bytes_hex"]))
        preimage = hashes.decode_policy_hash_v1_preimage(policy.encode())
        return {
            "encode_round_trip": policy.encode() == h(dp["scale_bytes_hex"]),
            "version": policy.version == 1,
            "policy_class": policy.policy_class == objects.DecodePolicyClass.text_generation(),
            "output_transform": policy.output_transform == objects.OutputTransform.identity(),
            "hash_preimage": preimage == h(dp["hash_preimage_hex"]),
            "sha256": hashes.decode_policy_hash_v1(policy.encode()) == h(dp["sha256_hex"]),
            "policy_hash_method": policy.policy_hash() == h(dp["sha256_hex"]),
        }

    vectors.append(_try_vector("decode_policy_v1", build_decode_policy, "decodes and hashes"))

    def build_task_hash() -> dict[str, bool]:
        th = corpus["direct_rail_v1"]["task_hash"]
        inputs = th["inputs"]
        args = (
            h(inputs["genesis_hash_hex"]),
            h(inputs["agent_account_id32_hex"]),
            int(inputs["nonce"]),
            h(inputs["model_hash_hex"]),
            h(inputs["payload_hash_hex"]),
            h(inputs["commit_hash_hex"]),
        )
        preimage = hashes.task_hash_v1_preimage(*args)
        return {
            "preimage_bytes": preimage == h(th["preimage_hex"]),
            "digest": hashes.task_hash_v1(*args) == h(th["hash_hex"]),
        }

    vectors.append(
        _try_vector("direct_rail_v1.task_hash", build_task_hash, "preimage and digest match")
    )

    def build_report_data() -> dict[str, bool]:
        dr = corpus["direct_rail_v1"]
        inputs = dr["inputs"]
        args = (
            h(inputs["task_hash_hex"]),
            int(inputs["gn_weight"]),
            int(inputs["latency_ms"]),
            h(inputs["model_hash_hex"]),
            h(inputs["output_hash_hex"]),
            h(inputs["decode_policy_hash_hex"]),
            int(inputs["tee_type"]["scale_tag"]),
        )
        preimage = hashes.report_data_v1_preimage(*args)
        result = hashes.report_data_v1(*args)
        return {
            "preimage_bytes": preimage == h(dr["report_data_preimage_hex"]),
            "result": result == h(dr["report_data_hex"]),
            "trailing_32_zero": result[32:] == b"\x00" * 32,
        }

    vectors.append(
        _try_vector("direct_rail_v1.report_data", build_report_data, "preimage/result match")
    )

    def build_validator_attestation() -> dict[str, bool]:
        dr = corpus["direct_rail_v1"]
        attestation = objects.ValidatorAttestation.decode(h(dr["validator_attestation_scale_hex"]))
        return {
            "tee_type": attestation.tee_type == objects.TeeType.INTEL_TDX,
            "quote_verified": attestation.quote_verified is True,
            "event_log_verified": attestation.event_log_verified is True,
            "validator_id": attestation.validator_id == h(dr["validator_id_hex"]),
            "signature": attestation.signature == h(dr["validator_signature_hex"]),
            "signable_bytes": (
                attestation.signable_bytes() == h(dr["validator_attestation_signable_hex"])
            ),
            "report_data": attestation.report_data() == h(dr["report_data_hex"]),
        }

    vectors.append(
        _try_vector(
            "direct_rail_v1.validator_attestation",
            build_validator_attestation,
            "decodes, signable bytes and report_data match",
        )
    )

    def build_no_verifier() -> dict[str, bool]:
        cc = corpus["compute_channel_v1"]
        rc = cc["receipt"]
        receipt = objects.ReceiptV1(
            channel_id=h(rc["inputs"]["channel_id_hex"]),
            final_root=h(rc["inputs"]["final_root_hex"]),
            aggregate_gn=int(rc["inputs"]["aggregate_gn"]),
            payable=int(rc["inputs"]["payable"]),
        )
        receipt_check = verify.check_receipt_signature(
            receipt, h(rc["public_key_hex"]), h(rc["signature_hex"])
        )
        v3sig = cc["v3_leaf_signature"]
        leaf_check = verify.check_leaf_signature(
            h(v3sig["leaf_hash_hex"]), h(v3sig["public_key_hex"]), h(v3sig["signature_hex"])
        )
        dr = corpus["direct_rail_v1"]
        attestation = objects.ValidatorAttestation.decode(h(dr["validator_attestation_scale_hex"]))
        validator_check = verify.check_validator_signature(attestation, verify.NoVerifier())
        return {
            "receipt_not_verified": receipt_check.status == "not_verified",
            "leaf_not_verified": leaf_check.status == "not_verified",
            "validator_not_verified": validator_check.status == "not_verified",
        }

    vectors.append(
        _try_vector(
            "signature_seam.no_verifier",
            build_no_verifier,
            "every check reports not_verified, never accepted",
        )
    )

    def build_stub_verifier() -> dict[str, bool]:
        cc = corpus["compute_channel_v1"]
        rc = cc["receipt"]
        receipt = objects.ReceiptV1(
            channel_id=h(rc["inputs"]["channel_id_hex"]),
            final_root=h(rc["inputs"]["final_root_hex"]),
            aggregate_gn=int(rc["inputs"]["aggregate_gn"]),
            payable=int(rc["inputs"]["payable"]),
        )
        receipt_pub, receipt_sig = h(rc["public_key_hex"]), h(rc["signature_hex"])
        v3sig = cc["v3_leaf_signature"]
        leaf_hash_value = h(v3sig["leaf_hash_hex"])
        leaf_pub, leaf_sig = h(v3sig["public_key_hex"]), h(v3sig["signature_hex"])
        dr = corpus["direct_rail_v1"]
        attestation = objects.ValidatorAttestation.decode(h(dr["validator_attestation_scale_hex"]))
        stub = verify.StubVerifier(
            [
                (receipt_pub, receipt.message(), receipt_sig),
                (leaf_pub, leaf_hash_value, leaf_sig),
                (attestation.validator_id, attestation.signable_bytes(), attestation.signature),
            ]
        )
        receipt_check = verify.check_receipt_signature(receipt, receipt_pub, receipt_sig, stub)
        leaf_check = verify.check_leaf_signature(leaf_hash_value, leaf_pub, leaf_sig, stub)
        validator_check = verify.check_validator_signature(attestation, stub)
        return {
            "receipt_accepted": receipt_check.status == "accepted",
            "leaf_accepted": leaf_check.status == "accepted",
            "validator_accepted": validator_check.status == "accepted",
        }

    vectors.append(
        _try_vector(
            "signature_seam.stub_verifier",
            build_stub_verifier,
            "the verifier seam accepts the corpus triples when given a stub that recognizes "
            "exactly them (a test device: proves the seam, not any sr25519 signature)",
        )
    )
    return vectors


def _build_vectors(corpus: Corpus) -> list[dict[str, object]]:
    vectors = _vectors_codec(corpus) + _vectors_compute_channel(corpus) + _vectors_misc(corpus)
    vectors.sort(key=lambda v: str(v["id"]))
    return vectors


# ================================================================================================
# Negative cases (all 14, `tests/test_flop_wire_corpus.py` section 13)
# ================================================================================================


def _reject_case(case: Case, action: Callable[[], object]) -> Case:
    """Mirrors the `pytest.raises(WireDecodeError)` pattern most `test_negative_case_*` functions
    use: `action` performs the exact decode/verify call the test performs; `WireDecodeError` is
    the expected outcome for every case this helper is used for."""
    try:
        action()
    except WireDecodeError as exc:
        return {
            "id": case["id"],
            "expected": case["expected"],
            "observed": f"rejected: {exc.reason}",
            "matches_expected": _reject_match(str(case["expected"]), exc.reason),
        }
    return {
        "id": case["id"],
        "expected": case["expected"],
        "observed": "not_rejected: the decoder accepted these bytes without raising",
        "matches_expected": False,
    }


def _case_invalid_receipt_signature(corpus: Corpus, case: Case) -> Case:
    rc = corpus["compute_channel_v1"]["receipt"]
    receipt = objects.ReceiptV1(
        channel_id=h(rc["inputs"]["channel_id_hex"]),
        final_root=h(rc["inputs"]["final_root_hex"]),
        aggregate_gn=int(rc["inputs"]["aggregate_gn"]),
        payable=int(rc["inputs"]["payable"]),
    )
    # Deliberately NOT the test file's injected StubVerifier: this report only claims what
    # production code (no injected sr25519 verifier) can actually show.
    check = verify.check_receipt_signature(receipt, h(rc["public_key_hex"]), h(case["bytes_hex"]))
    return {
        "id": case["id"],
        "expected": case["expected"],
        "observed": (
            "not_verifiable: no sr25519 implementation in this package; "
            f"check_receipt_signature reports status={check.status!r} without an injected "
            "verifier, never accepted or rejected"
        ),
        "matches_expected": None,
        "note": (
            "the corpus test proves rejection using an injected StubVerifier (a test device); "
            "this report does not treat that as evidence of a real cryptographic result"
        ),
    }


def _case_invalid_validator_signature(corpus: Corpus, case: Case) -> Case:
    dr = corpus["direct_rail_v1"]
    attestation = objects.ValidatorAttestation.decode(h(dr["validator_attestation_scale_hex"]))
    check = verify.check_validator_signature(attestation)
    return {
        "id": case["id"],
        "expected": case["expected"],
        "observed": (
            "not_verifiable: no sr25519 implementation in this package; "
            f"check_validator_signature reports status={check.status!r} without an injected "
            "verifier, never accepted or rejected"
        ),
        "matches_expected": None,
        "note": (
            "the corpus test proves rejection using an injected StubVerifier (a test device); "
            "this report does not treat that as evidence of a real cryptographic result"
        ),
    }


def _case_wrong_path_orientation(corpus: Corpus, case: Case) -> Case:
    turn = objects.VerifiedTurn.decode(h(case["bytes_hex"]))
    cc = corpus["compute_channel_v1"]
    channel_id = h(cc["leaf_inputs"]["channel_id_hex"])
    final_root = h(cc["merkle"]["root_hex"])
    try:
        result = settle.verified_work_from_turns(
            [turn], channel_id=channel_id, final_root=final_root, channel_has_decode_policy=True
        )
    except WireDecodeError as exc:
        return {
            "id": case["id"],
            "expected": case["expected"],
            "observed": f"rejected: {exc.reason}",
            "matches_expected": _reject_match(str(case["expected"]), exc.reason),
        }
    # Derive, never assume, WHY the flip is undetectable -- the same three facts
    # `tests/test_flop_wire_corpus.py::test_negative_case_wrong_path_orientation_root_is_unaffected`
    # proves: the flipped step's sibling is the leaf's own hash, the flag was flipped to True, and
    # the recomputed root still equals the committed root. `_build_negative_cases` emits the
    # specific "orientation-blind" deviation text only when all three hold.
    leaf_hash_value = turn.leaf_hash(channel_id)
    sibling, sibling_is_left = turn.merkle_path[0]
    self_duplicate = sibling == leaf_hash_value
    root_unchanged = hashes.merkle_root_from_path(leaf_hash_value, turn.merkle_path) == final_root
    cause_verified = self_duplicate and sibling_is_left is True and root_unchanged
    return {
        "id": case["id"],
        "expected": case["expected"],
        "observed": (
            "not_rejected: verified_work_from_turns accepted the turn "
            f"(turn_count={result.turn_count}); see deviations[0]"
        ),
        "matches_expected": False,
        "note": (
            f"{'verified' if cause_verified else 'UNVERIFIED'} cause: step-0 sibling == leaf hash "
            f"{self_duplicate}; sibling_is_left {sibling_is_left}; recomputed root == final_root "
            f"{root_unchanged}"
        ),
    }


def _case_wrong_genesis_network(corpus: Corpus, case: Case) -> Case:
    ci = corpus["compute_channel_v1"]["channel_id"]
    baseline = h(ci["hash_hex"])
    inputs = ci["inputs"]
    alt = hashes.channel_id_v1(
        h(case["bytes_hex"]),
        h(inputs["agent_account_id32_hex"]),
        h(inputs["miner_account_id32_hex"]),
        int(inputs["nonce"]),
    )
    differs = alt != baseline
    matches = str(case["expected"]) == "different channel_id" and differs
    return {
        "id": case["id"],
        "expected": case["expected"],
        "observed": (
            "not_rejected: recomputed channel_id through the real builder with the mutated "
            f"genesis_hash substituted -- {'differs from' if differs else 'equals'} the baseline"
        ),
        "matches_expected": matches,
        "note": "not a decode rejection: a different channel_id is the expected, correct outcome",
    }


def _case_wrong_session(corpus: Corpus, case: Case) -> Case:
    ci = corpus["compute_channel_v1"]["channel_id"]
    baseline = h(ci["hash_hex"])
    inputs = ci["inputs"]
    alt = hashes.channel_id_v1(
        h(inputs["genesis_hash_hex"]),
        h(case["bytes_hex"]),
        h(inputs["miner_account_id32_hex"]),
        int(inputs["nonce"]),
    )
    differs = alt != baseline
    matches = str(case["expected"]) == "different channel_id" and differs
    return {
        "id": case["id"],
        "expected": case["expected"],
        "observed": (
            "not_rejected: recomputed channel_id through the real builder with the mutated value "
            f"substituted for agent -- {'differs from' if differs else 'equals'} the baseline"
        ),
        "matches_expected": matches,
        "note": (
            'mutation is "change agent/miner/nonce" but bytes_hex is a single H256-sized value; '
            "substituted for agent here, mirroring the test"
        ),
    }


def _case_duplicate_turn_index(corpus: Corpus, case: Case) -> Case:
    turns = objects.decode_verified_turn_vec(h(case["bytes_hex"]))
    cc = corpus["compute_channel_v1"]
    channel_id = h(cc["leaf_inputs"]["channel_id_hex"])
    final_root = h(cc["merkle"]["root_hex"])
    if len(turns) != 2:  # the test asserts the mutation is exactly two turns with one index
        return {
            "id": case["id"],
            "expected": case["expected"],
            "observed": f"not_rejected: decoded {len(turns)} turns, the corpus mutation has 2",
            "matches_expected": False,
        }
    return _reject_case(
        case,
        lambda: settle.verified_work_from_turns(
            turns, channel_id=channel_id, final_root=final_root, channel_has_decode_policy=True
        ),
    )


def _case_legacy_leaf_current_channel(corpus: Corpus, case: Case) -> Case:
    turn = objects.VerifiedTurn.decode(h(case["bytes_hex"]))
    cc = corpus["compute_channel_v1"]
    channel_id = h(cc["leaf_inputs"]["channel_id_hex"])
    final_root = h(cc["merkle"]["root_hex"])
    # Accepted structurally when the channel has NOT pinned a decode policy -- channel STATE, only
    # noted here, not re-derived; the corpus's own "reject" expectation is for the pinned-policy
    # case below.
    accepted = settle.verified_work_from_turns(
        [turn], channel_id=channel_id, final_root=final_root, channel_has_decode_policy=False
    )
    accepted_ok = accepted.turn_count == 1
    try:
        settle.verified_work_from_turns(
            [turn], channel_id=channel_id, final_root=final_root, channel_has_decode_policy=True
        )
    except WireDecodeError as exc:
        matches = accepted_ok and _reject_match(str(case["expected"]), exc.reason)
        return {
            "id": case["id"],
            "expected": case["expected"],
            "observed": f"rejected: {exc.reason}",
            "matches_expected": matches,
            "note": (
                "rejected only once channel_has_decode_policy=True (channel STATE, not a "
                "wire-bytes violation); accepted (turn_count=1) when False"
            ),
        }
    return {
        "id": case["id"],
        "expected": case["expected"],
        "observed": "not_rejected: accepted even with channel_has_decode_policy=True (unexpected)",
        "matches_expected": False,
    }


def _case_legacy_receipt_current_channel(corpus: Corpus, case: Case) -> Case:
    decoded, signature = objects.decode_receipt_with_signature(h(case["bytes_hex"]))
    rc = corpus["compute_channel_v1"]["receipt"]
    v1_receipt = objects.ReceiptV1(
        channel_id=h(rc["inputs"]["channel_id_hex"]),
        final_root=h(rc["inputs"]["final_root_hex"]),
        aggregate_gn=int(rc["inputs"]["aggregate_gn"]),
        payable=int(rc["inputs"]["payable"]),
    )
    # The corpus expects `BadReceiptSignature`, i.e. a signature outcome. This package cannot
    # produce one (no sr25519); the corpus test proves the rejection only through an injected
    # StubVerifier that recognizes exactly the v1-domain triple -- a test device. The report
    # records what IS independently checkable (the legacy message shape) and declares the
    # signature verdict not verifiable, the same rule the two `invalid_*_signature` cases follow.
    message_shape_ok = decoded.message() == v1_receipt.message()[29:]
    check = verify.check_receipt_signature(
        decoded, h(rc["public_key_hex"]), signature, verify.NoVerifier()
    )
    return {
        "id": case["id"],
        "expected": case["expected"],
        "observed": (
            "not_verifiable: no sr25519 implementation in this package; "
            f"check_receipt_signature reports status={check.status!r} (never accepted)"
        ),
        "matches_expected": None,
        "note": (
            "decodes as LegacyReceipt (untagged 96 B message) whose message equals the v1 "
            f"message with the 29 B domain/version prefix stripped: {message_shape_ok}; the "
            "corpus test proves the rejection only with an injected StubVerifier (a test device); "
            "a real verifier is required to confirm BadReceiptSignature"
        ),
    }


def _build_negative_case(corpus: Corpus, case: Case) -> Case:
    case_id = str(case["id"])
    if case_id == "invalid_receipt_signature":
        return _case_invalid_receipt_signature(corpus, case)
    if case_id == "invalid_validator_signature":
        return _case_invalid_validator_signature(corpus, case)
    if case_id == "wrong_path_orientation":
        return _case_wrong_path_orientation(corpus, case)
    if case_id == "wrong_genesis_network":
        return _case_wrong_genesis_network(corpus, case)
    if case_id == "wrong_session":
        return _case_wrong_session(corpus, case)
    if case_id == "unknown_leaf_enum":
        return _reject_case(case, lambda: objects.decode_leaf_version(Reader(h(case["bytes_hex"]))))
    if case_id == "unknown_retention_enum":
        return _reject_case(
            case, lambda: objects.decode_retention_class(Reader(h(case["bytes_hex"])))
        )
    if case_id in ("truncated_fcc4", "trailing_fcc4", "unknown_fcc_version"):
        return _reject_case(case, lambda: objects.Fcc4Transcript.decode(h(case["bytes_hex"])))
    if case_id == "duplicate_turn_index":
        return _case_duplicate_turn_index(corpus, case)
    if case_id == "wrong_leaf_version":
        return _reject_case(case, lambda: objects.VerifiedTurn.decode(h(case["bytes_hex"])))
    if case_id == "legacy_leaf_current_channel":
        return _case_legacy_leaf_current_channel(corpus, case)
    if case_id == "legacy_receipt_current_channel":
        return _case_legacy_receipt_current_channel(corpus, case)
    return {
        "id": case_id,
        "expected": case.get("expected", ""),
        "observed": "not_rejected: no report logic implemented for this corpus case id",
        "matches_expected": False,
        "note": (
            "scripts/make_wire_conformance_report.py has no handler for this id (corpus drift) "
            "-- update it alongside tests/test_flop_wire_corpus.py"
        ),
    }


def _build_negative_cases(corpus: Corpus) -> tuple[list[Case], list[Case]]:
    cases: list[Case] = []
    for case in corpus["negative_cases"]:
        try:
            entry = _build_negative_case(corpus, case)
        except Exception as exc:  # a report must never crash on one bad case
            entry = {
                "id": case["id"],
                "expected": case.get("expected", ""),
                "observed": (
                    f"not_rejected: internal error building this case: {type(exc).__name__}: {exc}"
                ),
                "matches_expected": False,
            }
        cases.append(entry)

    deviations: list[Case] = []
    wrong_path = next((c for c in cases if c["id"] == "wrong_path_orientation"), None)
    if wrong_path is not None and wrong_path["matches_expected"] is False:
        deviations.append(
            {
                "case": "wrong_path_orientation",
                "expected": wrong_path["expected"],
                "observed": (
                    "root unchanged: the step-0 sibling is the leaf's own duplicate, so "
                    "blake2_256(X||X) is orientation-blind"
                    if str(wrong_path.get("note", "")).startswith("verified cause")
                    else "not_rejected for a cause this run could NOT re-derive: "
                    + str(wrong_path.get("note", "no note"))
                ),
                "upstream_issue": _UPSTREAM_ISSUE,
            }
        )
    return cases, deviations


# ================================================================================================
# Assembly
# ================================================================================================


def build_report(corpus_bytes: bytes) -> dict[str, object]:
    """Pure: `corpus_bytes` in, the full report `dict` out. All I/O (reading the corpus file,
    writing the report) lives in `main`, not here."""
    corpus: Corpus = json.loads(corpus_bytes.decode("utf-8"))

    vectors = _build_vectors(corpus)
    negative_cases, deviations = _build_negative_cases(corpus)

    vectors_ok = sum(1 for v in vectors if v["result"] == "ok")
    not_verifiable = sum(1 for c in negative_cases if c["matches_expected"] is None)
    matching = sum(1 for c in negative_cases if c["matches_expected"] is True)

    return {
        "schema": "openagentsearch.flop-wire-conformance-report/1",
        "corpus": {
            "repository": _CORPUS_REPOSITORY,
            "commit": _CORPUS_COMMIT,
            "path": _CORPUS_REL_PATH,
            "profile": corpus["profile"],
            "status": corpus["status"],
            "sha256": hashlib.sha256(corpus_bytes).hexdigest(),
            "bytes": len(corpus_bytes),
        },
        "implementation": {
            "repository": _IMPL_REPOSITORY,
            "module": _IMPL_MODULE,
            "language": "python-stdlib",
            "signature_verification": (
                "none (sr25519 not in the standard library; every signature check reports "
                "not_verified, never a pass)"
            ),
        },
        "regenerate": _REGENERATE_CMD,
        "vectors": vectors,
        "negative_cases": negative_cases,
        "summary": {
            "vectors": len(vectors),
            "vectors_ok": vectors_ok,
            "negative_cases": len(negative_cases),
            "matching": matching,
            "not_verifiable": not_verifiable,
            "deviations": len(deviations),
        },
        "deviations": deviations,
    }


def to_bytes(report: dict[str, object]) -> bytes:
    """`report` -> the exact bytes written to `--out` (or stdout): `sort_keys=True`, `indent=2`,
    `ensure_ascii=False`, a single trailing LF -- byte-deterministic across runs and machines (no
    timestamps, no absolute paths, no environment data ever enters `report`)."""
    text = json.dumps(report, sort_keys=True, indent=2, ensure_ascii=False)
    return (text + "\n").encode("utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--corpus",
        default=DEFAULT_CORPUS,
        help="path to the FLOP wire-format-v1 corpus JSON (default: %(default)s)",
    )
    parser.add_argument(
        "--out", default=None, help="path to write the report JSON to (default: stdout)"
    )
    args = parser.parse_args(argv)

    corpus_path = Path(args.corpus)
    try:
        corpus_bytes = corpus_path.read_bytes()
    except OSError as exc:
        print(f"cannot read corpus at {corpus_path}: {exc}", file=sys.stderr)
        return 2

    try:
        report = build_report(corpus_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        print(
            f"corpus at {corpus_path} is not a valid wire-format-v1 corpus: {exc}",
            file=sys.stderr,
        )
        return 2

    output = to_bytes(report)
    if args.out is None:
        sys.stdout.buffer.write(output)
    else:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
