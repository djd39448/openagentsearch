"""Package B3: FLOP v1 wire-format decoders (`openagentsearch.flop.wire`), driven end to end
against the vendored public corpus `tests/fixtures/flop/wire-format-v1.json` (CC BY 4.0, see
`tests/fixtures/flop/NOTICE.txt`). The corpus's own sha256 is pinned below and re-checked on every
run -- a corpus edit fails this suite loudly rather than silently changing what "passing" means.

One documented deviation from the corpus's own `negative_cases[*].expected` text, discovered while
writing this file (not assumed from the brief): `wrong_path_orientation` mutates the
`sibling_is_left` flag of the FIRST Merkle-path step for `compute_channel_v1`'s V3 example -- but
that step's sibling is a self-duplicate of the leaf's own hash (leaf index 2 is the odd-one-out of
a 3-leaf tree, duplicated against itself per `odd_node_behavior: "duplicate last"`). Combining two
identical 32-byte values is byte-for-byte the same regardless of which is called "left": there is
no implementation, correct or not, under which flipping that specific flag changes the recomputed
root. `test_negative_case_wrong_path_orientation_root_is_unaffected` proves this with the actual
corpus bytes rather than asserting it from theory. See `deviations` in this package's BUILDSPEC
return value for the full writeup."""

import hashlib
import json
from pathlib import Path

import pytest

from openagentsearch.flop.wire import hashes, objects, settle, verify
from openagentsearch.flop.wire.scale import (
    Reader,
    WireDecodeError,
    decode_compact_u32,
    encode_compact_u32,
)

CORPUS_PATH = Path(__file__).resolve().parent / "fixtures" / "flop" / "wire-format-v1.json"
CORPUS_SHA256 = "80d4a7e70f984342eb474ae5285a17a6b9348eca887e1689b15e641922051d93"
_RAW = CORPUS_PATH.read_bytes()
CORPUS = json.loads(_RAW.decode("utf-8"))


def h(hex_str: str) -> bytes:
    return bytes.fromhex(hex_str)


def _neg(case_id: str) -> dict:
    for case in CORPUS["negative_cases"]:
        if case["id"] == case_id:
            return case
    raise KeyError(case_id)


# Every top-level corpus key and every negative_cases[*].id this file exercises -- kept in sync by
# hand; test_every_corpus_section_is_referenced fails loudly if the corpus grows a key or a
# negative case this file does not yet know about.
_REFERENCED_TOP_LEVEL_KEYS = frozenset(
    {
        "$schema",
        "codec",
        "compute_channel_v1",
        "coverage",
        "data_ref_v1",
        "decode_policy_v1",
        "direct_rail_v1",
        "generation",
        "negative_cases",
        "profile",
        "status",
    }
)
_REFERENCED_NEGATIVE_CASE_IDS = frozenset(
    {
        "unknown_leaf_enum",
        "unknown_retention_enum",
        "truncated_fcc4",
        "trailing_fcc4",
        "unknown_fcc_version",
        "duplicate_turn_index",
        "wrong_path_orientation",
        "wrong_genesis_network",
        "wrong_session",
        "wrong_leaf_version",
        "invalid_receipt_signature",
        "invalid_validator_signature",
        "legacy_leaf_current_channel",
        "legacy_receipt_current_channel",
    }
)


# 0. The corpus fixture itself is pinned by sha256 -----------------------------------------------


def test_corpus_sha256_is_pinned():
    assert hashlib.sha256(_RAW).hexdigest() == CORPUS_SHA256


def test_corpus_metadata_fields_present():
    assert CORPUS["$schema"] == "wire-format-v1.schema.json"
    assert CORPUS["profile"] == "flop-wire-v1"
    assert CORPUS["status"] == "public-canonical"
    assert set(CORPUS["generation"]) == {"public_vectors", "signature", "vectors"}


def test_every_corpus_section_is_referenced():
    """Fails loudly if the corpus gains a top-level key or a negative case this file has not been
    updated to cover."""
    assert set(CORPUS.keys()) == _REFERENCED_TOP_LEVEL_KEYS
    actual_ids = {case["id"] for case in CORPUS["negative_cases"]}
    assert actual_ids == _REFERENCED_NEGATIVE_CASE_IDS


# 1. codec.malformed_compact / scale_compact_u32 / scale_bool ------------------------------------

_MALFORMED_COMPACT_REASON_MAP = {
    "truncated": "truncated",
    "truncated mode 1": "truncated",
    "truncated mode 2": "truncated",
    "overlong zero": "overlong",
    "overlong big mode": "overlong",
    "exceeds u32": "exceeds u32",
}


@pytest.mark.parametrize("case", CORPUS["codec"]["malformed_compact"], ids=lambda c: c["reason"])
def test_codec_malformed_compact(case):
    expected_reason = _MALFORMED_COMPACT_REASON_MAP[case["reason"]]
    with pytest.raises(WireDecodeError) as exc_info:
        decode_compact_u32(h(case["bytes_hex"]))
    assert exc_info.value.reason == expected_reason
    assert str(exc_info.value).startswith(expected_reason)


@pytest.mark.parametrize(
    "case", CORPUS["codec"]["scale_compact_u32"], ids=lambda c: str(c["value"])
)
def test_codec_scale_compact_u32(case):
    value = decode_compact_u32(h(case["bytes_hex"]))
    assert value == case["value"]
    assert encode_compact_u32(value) == h(case["bytes_hex"])


def test_codec_scale_bool():
    scale_bool = CORPUS["codec"]["scale_bool"]
    assert Reader(h(scale_bool["false"])).bool_() is False
    assert Reader(h(scale_bool["true"])).bool_() is True
    assert scale_bool["other"] == "reject"
    with pytest.raises(WireDecodeError) as exc_info:
        Reader(bytes([0x02])).bool_()
    assert exc_info.value.reason == "invalid bool"


def test_codec_fixed_integers_description_present():
    assert "little-endian" in CORPUS["codec"]["fixed_integers"]


# 2. compute_channel_v1.channel_id ----------------------------------------------------------------


def test_channel_id_v1_preimage_and_hash():
    ci = CORPUS["compute_channel_v1"]["channel_id"]
    inputs = ci["inputs"]
    preimage = hashes.channel_id_v1_preimage(
        h(inputs["genesis_hash_hex"]),
        h(inputs["agent_account_id32_hex"]),
        h(inputs["miner_account_id32_hex"]),
        inputs["nonce"],
    )
    assert preimage == h(ci["preimage_hex"])
    digest = hashes.channel_id_v1(
        h(inputs["genesis_hash_hex"]),
        h(inputs["agent_account_id32_hex"]),
        h(inputs["miner_account_id32_hex"]),
        inputs["nonce"],
    )
    assert digest == h(ci["hash_hex"])


def test_domains_ascii_strings_present():
    domains = CORPUS["compute_channel_v1"]["domains"]
    assert domains["channel_id_ascii"] == "FLOP/COMPUTE_CHANNEL/ID"
    assert domains["receipt_ascii"] == "FLOP/COMPUTE_CHANNEL/RECEIPT"


# 3. compute_channel_v1.leaf_versions[*] ----------------------------------------------------------

_EXPECTED_LEAF_PREIMAGE_LEN = {"V0": 116, "V1": 140, "V2": 172, "V3": 236}


class _Turn:
    """A minimal stand-in satisfying `hashes.LeafFields`, built from `leaf_inputs` -- used only to
    exercise `hashes.leaf_preimage`/`leaf_hash` directly against `leaf_versions[*]`, independent of
    `objects.VerifiedTurn`."""

    def __init__(self, leaf_inputs: dict):
        self.turn_index = leaf_inputs["turn_index"]
        self.h_in = h(leaf_inputs["h_in_hex"])
        self.h_out = h(leaf_inputs["h_out_hex"])
        self.g_n = int(leaf_inputs["g_n"])
        self.decode_policy_hash = h(leaf_inputs["decode_policy_hash_hex"])
        self.h_ids = h(leaf_inputs["h_ids_hex"])
        self.toploc_commitment_hash = h(leaf_inputs["toploc_commitment_hash_hex"])
        self.miner_recv_ms = int(leaf_inputs["miner_recv_ms"])
        self.miner_done_ms = int(leaf_inputs["miner_done_ms"])
        self.latency_ms = leaf_inputs["latency_ms"]


@pytest.mark.parametrize(
    "entry", CORPUS["compute_channel_v1"]["leaf_versions"], ids=lambda e: e["version"]
)
def test_leaf_version_preimage_and_hash(entry):
    turn = _Turn(CORPUS["compute_channel_v1"]["leaf_inputs"])
    channel_id = h(CORPUS["compute_channel_v1"]["leaf_inputs"]["channel_id_hex"])
    version = entry["scale_tag"]
    preimage = hashes.leaf_preimage(version, channel_id, turn)
    assert len(preimage) == _EXPECTED_LEAF_PREIMAGE_LEN[entry["version"]]
    assert preimage == h(entry["preimage_hex"])
    assert hashes.leaf_hash(version, channel_id, turn) == h(entry["hash_hex"])


# 4. compute_channel_v1.merkle --------------------------------------------------------------------


def test_merkle_root_from_path_for_index_2():
    mk = CORPUS["compute_channel_v1"]["merkle"]
    v3_hash = h(CORPUS["compute_channel_v1"]["leaf_versions"][3]["hash_hex"])
    path = [(h(step["sibling_hex"]), step["sibling_is_left"]) for step in mk["path_for_index_2"]]
    root = hashes.merkle_root_from_path(v3_hash, path)
    assert root == h(mk["root_hex"])


def test_merkle_root_of_leaves_over_leaf_order():
    mk = CORPUS["compute_channel_v1"]["merkle"]
    leaf_versions = CORPUS["compute_channel_v1"]["leaf_versions"]
    by_version = {e["version"]: h(e["hash_hex"]) for e in leaf_versions}
    leaves = [by_version[version] for version in mk["leaf_order"]]
    assert mk["odd_node_behavior"] == "duplicate last"
    assert hashes.merkle_root_of_leaves(leaves) == h(mk["root_hex"])


# 5. compute_channel_v1.receipt -------------------------------------------------------------------


def test_receipt_v1_message_matches_preimage():
    rc = CORPUS["compute_channel_v1"]["receipt"]
    receipt = objects.ReceiptV1(
        channel_id=h(rc["inputs"]["channel_id_hex"]),
        final_root=h(rc["inputs"]["final_root_hex"]),
        aggregate_gn=rc["inputs"]["aggregate_gn"],
        payable=rc["inputs"]["payable"],
    )
    assert receipt.message() == h(rc["preimage_hex"])
    assert hashes.receipt_message_v1(
        receipt.channel_id, receipt.final_root, receipt.aggregate_gn, receipt.payable
    ) == h(rc["preimage_hex"])


def test_decode_receipt_with_signature_returns_receipt_v1():
    rc = CORPUS["compute_channel_v1"]["receipt"]
    blob = h(rc["preimage_hex"]) + h(rc["signature_hex"])
    decoded, signature = objects.decode_receipt_with_signature(blob)
    assert isinstance(decoded, objects.ReceiptV1)
    assert decoded.message() == h(rc["preimage_hex"])
    assert signature == h(rc["signature_hex"])


# 6. compute_channel_v1.verified_turn_v3_scale_hex -------------------------------------------------


def test_verified_turn_v3_decodes_and_proves_membership():
    cc = CORPUS["compute_channel_v1"]
    turn = objects.VerifiedTurn.decode(h(cc["verified_turn_v3_scale_hex"]))
    leaf_inputs = cc["leaf_inputs"]
    assert turn.leaf_version == objects.LeafVersion.V3
    assert turn.turn_index == leaf_inputs["turn_index"]
    assert turn.h_in == h(leaf_inputs["h_in_hex"])
    assert turn.h_out == h(leaf_inputs["h_out_hex"])
    assert turn.g_n == int(leaf_inputs["g_n"])
    assert turn.decode_policy_hash == h(leaf_inputs["decode_policy_hash_hex"])
    assert turn.h_ids == h(leaf_inputs["h_ids_hex"])
    assert turn.toploc_commitment_hash == h(leaf_inputs["toploc_commitment_hash_hex"])
    assert turn.miner_recv_ms == int(leaf_inputs["miner_recv_ms"])
    assert turn.miner_done_ms == int(leaf_inputs["miner_done_ms"])
    assert turn.latency_ms == leaf_inputs["latency_ms"]
    assert turn.enclave_sig == h(cc["v3_leaf_signature"]["signature_hex"])

    channel_id = h(leaf_inputs["channel_id_hex"])
    leaf_hash = turn.leaf_hash(channel_id)
    assert leaf_hash == h(cc["leaf_versions"][3]["hash_hex"])
    root = hashes.merkle_root_from_path(leaf_hash, turn.merkle_path)
    assert root == h(cc["merkle"]["root_hex"])


def test_v3_leaf_signature_hash_matches_leaf_versions():
    cc = CORPUS["compute_channel_v1"]
    assert cc["v3_leaf_signature"]["leaf_hash_hex"] == cc["leaf_versions"][3]["hash_hex"]


# 7. compute_channel_v1.fcc4_transcript_blob_hex ----------------------------------------------


def test_fcc4_transcript_decodes_one_v3_turn():
    cc = CORPUS["compute_channel_v1"]
    transcript = objects.Fcc4Transcript.decode(h(cc["fcc4_transcript_blob_hex"]))
    assert transcript.channel_id == h(cc["leaf_inputs"]["channel_id_hex"])
    assert len(transcript.turns) == 1
    turn = transcript.turns[0]
    leaf_inputs = cc["leaf_inputs"]
    assert turn.leaf_version == 3
    assert turn.turn_index == leaf_inputs["turn_index"]
    assert turn.h_in == h(leaf_inputs["h_in_hex"])
    assert turn.h_out == h(leaf_inputs["h_out_hex"])
    assert turn.g_n == int(leaf_inputs["g_n"])
    assert turn.has_policy is True
    assert turn.policy_hash == h(leaf_inputs["decode_policy_hash_hex"])
    assert turn.h_ids == h(leaf_inputs["h_ids_hex"])
    assert turn.toploc_hash == h(leaf_inputs["toploc_commitment_hash_hex"])
    assert turn.miner_recv_ms == int(leaf_inputs["miner_recv_ms"])
    assert turn.miner_done_ms == int(leaf_inputs["miner_done_ms"])
    assert turn.latency_ms == leaf_inputs["latency_ms"]
    assert turn.enclave_sig == h(cc["v3_leaf_signature"]["signature_hex"])
    assert turn.has_ack is False
    assert turn.send_ms is None and turn.receive_ms is None and turn.agent_sig is None


# 8. coverage table ---------------------------------------------------------------------------


def test_coverage_table_shape():
    coverage = CORPUS["coverage"]
    assert len(coverage) == 6
    for entry in coverage:
        assert set(entry) == {"appendix", "consumer", "family"}


# 9. data_ref_v1 -------------------------------------------------------------------------------


def test_data_ref_v1_decode_and_encode_round_trip():
    drf = CORPUS["data_ref_v1"]
    decoded = objects.DataRef.decode(h(drf["scale_bytes_hex"]))
    assert decoded.commitment == h(drf["input"]["commitment_hex"])
    assert decoded.provider_id == drf["input"]["provider_id"]
    assert decoded.retention == objects.RetentionClass.EPHEMERAL
    assert decoded.encode() == h(drf["scale_bytes_hex"])


def test_data_ref_v1_leased_round_trips_uncovered_by_corpus():
    """`Leased` has no positive corpus vector (only `Ephemeral=0` is exercised) -- this round-trips
    a `Leased` `DataRef` built by this test, not by the corpus."""
    commitment = h(CORPUS["data_ref_v1"]["input"]["commitment_hex"])
    built = objects.DataRef(
        commitment=commitment, provider_id=5, retention=objects.RetentionClass.LEASED
    )
    round_tripped = objects.DataRef.decode(built.encode())
    assert round_tripped == built


# 10. decode_policy_v1 --------------------------------------------------------------------------


def test_decode_policy_v1_decode_encode_and_hash():
    dp = CORPUS["decode_policy_v1"]
    policy = objects.DecodePolicy.decode(h(dp["scale_bytes_hex"]))
    assert policy.encode() == h(dp["scale_bytes_hex"])
    assert policy.version == 1
    assert policy.policy_class == objects.DecodePolicyClass.text_generation()
    assert policy.output_transform == objects.OutputTransform.identity()

    preimage = hashes.decode_policy_hash_v1_preimage(policy.encode())
    assert preimage == h(dp["hash_preimage_hex"])
    assert hashes.decode_policy_hash_v1(policy.encode()) == h(dp["sha256_hex"])
    assert policy.policy_hash() == h(dp["sha256_hex"])


# 11. direct_rail_v1 ----------------------------------------------------------------------------


def test_task_hash_v1_preimage_and_hash():
    th = CORPUS["direct_rail_v1"]["task_hash"]
    inputs = th["inputs"]
    preimage = hashes.task_hash_v1_preimage(
        h(inputs["genesis_hash_hex"]),
        h(inputs["agent_account_id32_hex"]),
        inputs["nonce"],
        h(inputs["model_hash_hex"]),
        h(inputs["payload_hash_hex"]),
        h(inputs["commit_hash_hex"]),
    )
    assert preimage == h(th["preimage_hex"])
    digest = hashes.task_hash_v1(
        h(inputs["genesis_hash_hex"]),
        h(inputs["agent_account_id32_hex"]),
        inputs["nonce"],
        h(inputs["model_hash_hex"]),
        h(inputs["payload_hash_hex"]),
        h(inputs["commit_hash_hex"]),
    )
    assert digest == h(th["hash_hex"])


def test_report_data_v1_preimage_and_result():
    dr = CORPUS["direct_rail_v1"]
    inputs = dr["inputs"]
    preimage = hashes.report_data_v1_preimage(
        h(inputs["task_hash_hex"]),
        inputs["gn_weight"],
        inputs["latency_ms"],
        h(inputs["model_hash_hex"]),
        h(inputs["output_hash_hex"]),
        h(inputs["decode_policy_hash_hex"]),
        inputs["tee_type"]["scale_tag"],
    )
    assert preimage == h(dr["report_data_preimage_hex"])
    result = hashes.report_data_v1(
        h(inputs["task_hash_hex"]),
        inputs["gn_weight"],
        inputs["latency_ms"],
        h(inputs["model_hash_hex"]),
        h(inputs["output_hash_hex"]),
        h(inputs["decode_policy_hash_hex"]),
        inputs["tee_type"]["scale_tag"],
    )
    assert result == h(dr["report_data_hex"])
    assert result[32:] == b"\x00" * 32


def test_validator_attestation_decode_signable_and_report_data():
    dr = CORPUS["direct_rail_v1"]
    attestation = objects.ValidatorAttestation.decode(h(dr["validator_attestation_scale_hex"]))
    assert attestation.tee_type == objects.TeeType.INTEL_TDX
    assert attestation.quote_verified is True
    assert attestation.event_log_verified is True
    assert attestation.validator_id == h(dr["validator_id_hex"])
    assert attestation.signature == h(dr["validator_signature_hex"])
    assert attestation.signable_bytes() == h(dr["validator_attestation_signable_hex"])
    assert attestation.report_data() == h(dr["report_data_hex"])


# 12. Signature seam: NoVerifier / StubVerifier / flipped signatures -----------------------------


def test_signature_seam_no_verifier_never_reports_accepted():
    cc = CORPUS["compute_channel_v1"]
    rc = cc["receipt"]
    receipt = objects.ReceiptV1(
        channel_id=h(rc["inputs"]["channel_id_hex"]),
        final_root=h(rc["inputs"]["final_root_hex"]),
        aggregate_gn=rc["inputs"]["aggregate_gn"],
        payable=rc["inputs"]["payable"],
    )
    receipt_check = verify.check_receipt_signature(
        receipt, h(rc["public_key_hex"]), h(rc["signature_hex"])
    )
    assert receipt_check.status == "not_verified"

    v3sig = cc["v3_leaf_signature"]
    leaf_check = verify.check_leaf_signature(
        h(v3sig["leaf_hash_hex"]), h(v3sig["public_key_hex"]), h(v3sig["signature_hex"])
    )
    assert leaf_check.status == "not_verified"

    dr = CORPUS["direct_rail_v1"]
    attestation = objects.ValidatorAttestation.decode(h(dr["validator_attestation_scale_hex"]))
    validator_check = verify.check_validator_signature(attestation, verify.NoVerifier())
    assert validator_check.status == "not_verified"


def test_signature_seam_stub_verifier_accepts_corpus_triples():
    cc = CORPUS["compute_channel_v1"]
    rc = cc["receipt"]
    receipt = objects.ReceiptV1(
        channel_id=h(rc["inputs"]["channel_id_hex"]),
        final_root=h(rc["inputs"]["final_root_hex"]),
        aggregate_gn=rc["inputs"]["aggregate_gn"],
        payable=rc["inputs"]["payable"],
    )
    receipt_pub, receipt_sig = h(rc["public_key_hex"]), h(rc["signature_hex"])

    v3sig = cc["v3_leaf_signature"]
    leaf_hash, leaf_pub, leaf_sig = (
        h(v3sig["leaf_hash_hex"]), h(v3sig["public_key_hex"]), h(v3sig["signature_hex"])
    )

    dr = CORPUS["direct_rail_v1"]
    attestation = objects.ValidatorAttestation.decode(h(dr["validator_attestation_scale_hex"]))

    stub = verify.StubVerifier(
        [
            (receipt_pub, receipt.message(), receipt_sig),
            (leaf_pub, leaf_hash, leaf_sig),
            (attestation.validator_id, attestation.signable_bytes(), attestation.signature),
        ]
    )

    receipt_check = verify.check_receipt_signature(receipt, receipt_pub, receipt_sig, stub)
    assert receipt_check.status == "accepted"
    assert verify.check_leaf_signature(leaf_hash, leaf_pub, leaf_sig, stub).status == "accepted"
    assert verify.check_validator_signature(attestation, stub).status == "accepted"


def test_negative_case_invalid_receipt_signature_is_rejected():
    cc = CORPUS["compute_channel_v1"]
    rc = cc["receipt"]
    receipt = objects.ReceiptV1(
        channel_id=h(rc["inputs"]["channel_id_hex"]),
        final_root=h(rc["inputs"]["final_root_hex"]),
        aggregate_gn=rc["inputs"]["aggregate_gn"],
        payable=rc["inputs"]["payable"],
    )
    receipt_pub = h(rc["public_key_hex"])
    stub = verify.StubVerifier([(receipt_pub, receipt.message(), h(rc["signature_hex"]))])
    flipped = h(_neg("invalid_receipt_signature")["bytes_hex"])
    check = verify.check_receipt_signature(receipt, receipt_pub, flipped, stub)
    assert check.status == "rejected"
    assert check.reason == "BadReceiptSignature"


def test_negative_case_invalid_validator_signature_is_rejected():
    dr = CORPUS["direct_rail_v1"]
    attestation = objects.ValidatorAttestation.decode(h(dr["validator_attestation_scale_hex"]))
    stub = verify.StubVerifier(
        [(attestation.validator_id, attestation.signable_bytes(), attestation.signature)]
    )
    flipped = h(_neg("invalid_validator_signature")["bytes_hex"])
    tampered = objects.ValidatorAttestation.decode(
        h(dr["validator_attestation_scale_hex"])[:211] + flipped
    )
    check = verify.check_validator_signature(tampered, stub)
    assert check.status == "rejected"
    assert check.reason == "BadValidatorSignature"


# 13. negative_cases[*] by id --------------------------------------------------------------------


def test_negative_case_unknown_leaf_enum():
    case = _neg("unknown_leaf_enum")
    with pytest.raises(WireDecodeError) as exc_info:
        objects.decode_leaf_version(Reader(h(case["bytes_hex"])))
    assert exc_info.value.reason == "unknown tag"


def test_negative_case_unknown_retention_enum():
    case = _neg("unknown_retention_enum")
    with pytest.raises(WireDecodeError) as exc_info:
        objects.decode_retention_class(Reader(h(case["bytes_hex"])))
    assert exc_info.value.reason == "unknown tag"


def test_negative_case_truncated_fcc4():
    case = _neg("truncated_fcc4")
    with pytest.raises(WireDecodeError) as exc_info:
        objects.Fcc4Transcript.decode(h(case["bytes_hex"]))
    assert exc_info.value.reason == "truncated"


def test_negative_case_trailing_fcc4():
    case = _neg("trailing_fcc4")
    with pytest.raises(WireDecodeError) as exc_info:
        objects.Fcc4Transcript.decode(h(case["bytes_hex"]))
    assert exc_info.value.reason == "trailing bytes"


def test_negative_case_unknown_fcc_version():
    case = _neg("unknown_fcc_version")
    with pytest.raises(WireDecodeError) as exc_info:
        objects.Fcc4Transcript.decode(h(case["bytes_hex"]))
    assert exc_info.value.reason == "unknown magic"


def test_negative_case_duplicate_turn_index():
    case = _neg("duplicate_turn_index")
    turns = objects.decode_verified_turn_vec(h(case["bytes_hex"]))
    assert len(turns) == 2
    cc = CORPUS["compute_channel_v1"]
    channel_id = h(cc["leaf_inputs"]["channel_id_hex"])
    final_root = h(cc["merkle"]["root_hex"])
    with pytest.raises(WireDecodeError) as exc_info:
        settle.verified_work_from_turns(
            turns, channel_id=channel_id, final_root=final_root, channel_has_decode_policy=True
        )
    assert exc_info.value.reason == "DuplicateVerifiedTurn"


def test_negative_case_wrong_path_orientation_root_is_unaffected():
    """DEVIATION from the corpus's own `expected: "reject LeafNotInRoot"` text for this id -- see
    the module docstring. The mutated step's sibling is a byte-for-byte self-duplicate of the leaf
    hash itself (this V3 leaf sits at the odd-one-out position of a 3-leaf tree); combining two
    identical 32-byte values is the same bytes regardless of declared orientation, so the
    recomputed root is provably unaffected by this specific flip. `verified_work_from_turns` does
    NOT raise for this input -- proven here, not assumed."""
    case = _neg("wrong_path_orientation")
    turn = objects.VerifiedTurn.decode(h(case["bytes_hex"]))
    cc = CORPUS["compute_channel_v1"]
    channel_id = h(cc["leaf_inputs"]["channel_id_hex"])
    final_root = h(cc["merkle"]["root_hex"])

    sibling, sibling_is_left = turn.merkle_path[0]
    assert sibling == turn.leaf_hash(channel_id), "the flipped step's sibling is the leaf itself"
    assert sibling_is_left is True  # flipped from False in the un-mutated V3 example

    root = hashes.merkle_root_from_path(turn.leaf_hash(channel_id), turn.merkle_path)
    assert root == final_root

    result = settle.verified_work_from_turns(
        [turn], channel_id=channel_id, final_root=final_root, channel_has_decode_policy=True
    )
    assert result.turn_count == 1


def test_negative_case_wrong_genesis_network_is_a_different_channel_id_not_a_rejection():
    case = _neg("wrong_genesis_network")
    assert case["expected"] == "different channel_id"
    baseline = CORPUS["compute_channel_v1"]["channel_id"]["hash_hex"]
    inputs = CORPUS["compute_channel_v1"]["channel_id"]["inputs"]
    # `bytes_hex` is the mutated genesis_hash itself (mutation: "change genesis_hash"), an
    # H256-sized value distinct from `inputs["genesis_hash_hex"]`. Recompute through the real
    # `channel_id_v1` builder with it substituted for the baseline genesis_hash (agent/miner/nonce
    # unchanged) so this exercises the decoder's domain separation, not just a fixture diff.
    alt = hashes.channel_id_v1(
        h(case["bytes_hex"]),
        h(inputs["agent_account_id32_hex"]),
        h(inputs["miner_account_id32_hex"]),
        inputs["nonce"],
    )
    assert alt != h(baseline)


def test_negative_case_wrong_session_is_a_different_channel_id_not_a_rejection():
    case = _neg("wrong_session")
    assert case["expected"] == "different channel_id"
    baseline = CORPUS["compute_channel_v1"]["channel_id"]["hash_hex"]
    inputs = CORPUS["compute_channel_v1"]["channel_id"]["inputs"]
    # mutation is "change agent/miner/nonce" but `bytes_hex` is a single H256-sized value, so the
    # corpus does not disambiguate which one field it replaces (agent and miner are both 32 B;
    # nonce is not). Substituting it for `agent` here still recomputes through the real
    # `channel_id_v1` builder and demonstrates the same domain-separation property the corpus
    # asserts (a different channel_id, not a rejection) rather than only diffing fixture strings.
    alt = hashes.channel_id_v1(
        h(inputs["genesis_hash_hex"]),
        h(case["bytes_hex"]),
        h(inputs["miner_account_id32_hex"]),
        inputs["nonce"],
    )
    assert alt != h(baseline)


def test_negative_case_wrong_leaf_version():
    case = _neg("wrong_leaf_version")
    with pytest.raises(WireDecodeError) as exc_info:
        objects.VerifiedTurn.decode(h(case["bytes_hex"]))
    assert exc_info.value.reason == "LeafFieldsInconsistent"


def test_negative_case_legacy_leaf_current_channel():
    case = _neg("legacy_leaf_current_channel")
    turn = objects.VerifiedTurn.decode(h(case["bytes_hex"]))
    assert turn.leaf_version in (objects.LeafVersion.V0, objects.LeafVersion.V1)

    cc = CORPUS["compute_channel_v1"]
    channel_id = h(cc["leaf_inputs"]["channel_id_hex"])
    final_root = h(cc["merkle"]["root_hex"])

    # Accepted structurally when the channel has NOT pinned a decode policy: no exception, and the
    # turn's own (genuinely valid, corpus-constructed) authentication path proves membership under
    # the same final_root.
    accepted = settle.verified_work_from_turns(
        [turn], channel_id=channel_id, final_root=final_root, channel_has_decode_policy=False
    )
    assert accepted.turn_count == 1

    # Rejected once the channel has pinned a decode policy: pre-policy leaf versions are no longer
    # accepted, even though the bytes are otherwise perfectly valid -- this is channel STATE
    # (caller-supplied), not a wire-bytes violation.
    with pytest.raises(WireDecodeError) as exc_info:
        settle.verified_work_from_turns(
            [turn], channel_id=channel_id, final_root=final_root, channel_has_decode_policy=True
        )
    assert exc_info.value.reason == "UnsupportedLeafVersion"


def test_negative_case_legacy_receipt_current_channel():
    case = _neg("legacy_receipt_current_channel")
    decoded, signature = objects.decode_receipt_with_signature(h(case["bytes_hex"]))
    assert isinstance(decoded, objects.LegacyReceipt)

    rc = CORPUS["compute_channel_v1"]["receipt"]
    v1_receipt = objects.ReceiptV1(
        channel_id=h(rc["inputs"]["channel_id_hex"]),
        final_root=h(rc["inputs"]["final_root_hex"]),
        aggregate_gn=rc["inputs"]["aggregate_gn"],
        payable=rc["inputs"]["payable"],
    )
    assert decoded.message() == v1_receipt.message()[29:]  # same 4 fields, domain/version stripped

    stub = verify.StubVerifier(
        [(h(rc["public_key_hex"]), v1_receipt.message(), h(rc["signature_hex"]))]
    )
    check = verify.check_receipt_signature(decoded, h(rc["public_key_hex"]), signature, stub)
    assert check.status == "rejected"
    assert check.reason == "BadReceiptSignature"
