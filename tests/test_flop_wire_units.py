"""Package B3: unit-level coverage for `openagentsearch.flop.wire` that the public corpus does not
itself exercise -- compact-integer edge cases and truncation at every fixed width, `bool`'s full
254-value rejection space (exhaustively enumerated -- see `Reader.bool_`'s docstring),
`finish()` trailing-bytes, enum-tag rejection shapes,
`PathTooLong`/`TooManyTurns`/`AggregateGnOverflow`, `merkle_root_of_leaves` on 0/1/2/3 leaves, and
`encode`/`decode` round-trips for `DataRef` and `DecodePolicy` (including the `class=Other(u16)`
and `output_transform=TransformId(H256)` variants the corpus's single `DecodePolicy::default()`
vector never exercises)."""

import pytest

from openagentsearch.flop.wire import hashes, objects, settle
from openagentsearch.flop.wire.scale import (
    Reader,
    WireDecodeError,
    decode_compact_u32,
    encode_compact_u32,
)
from openagentsearch.flop.wire.verify import NoVerifier, StubVerifier, check_leaf_signature

H256_A = bytes([0x11]) * 32
H256_B = bytes([0x22]) * 32
H256_C = bytes([0x33]) * 32
H256_D = bytes([0x44]) * 32
CHANNEL_ID = bytes([0xAB]) * 32


def _turn(
    turn_index: int,
    *,
    leaf_version: objects.LeafVersion = objects.LeafVersion.V0,
    g_n: int = 0,
    merkle_path: tuple = (),
    h_in: bytes = H256_A,
    h_out: bytes = H256_B,
) -> objects.VerifiedTurn:
    return objects.VerifiedTurn(
        leaf_version=leaf_version,
        turn_index=turn_index,
        h_in=h_in,
        h_out=h_out,
        g_n=g_n,
        decode_policy_hash=b"\x00" * 32,
        h_ids=b"\x00" * 32,
        toploc_commitment_hash=b"\x00" * 32,
        miner_recv_ms=0,
        miner_done_ms=0,
        latency_ms=0,
        enclave_sig=b"\x55" * 64,
        merkle_path=merkle_path,
    )


# 1. Compact<u32>: truncation at every mode's boundary --------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        b"",  # mode 0, 0 bytes: nothing to read at all
        bytes([0b01]),  # mode 1, 1 byte given, needs 2
        bytes([0b10]) + b"\x00",  # mode 2, 2 bytes given, needs 4
        bytes([0b10]) + b"\x00\x00",  # mode 2, 3 bytes given, needs 4
        bytes([0b11]) + b"\x00\x00\x00",  # mode 3 (n=4), 4 bytes given, needs 5
    ],
    ids=["empty", "mode1_short", "mode2_short_by_2", "mode2_short_by_1", "mode3_short"],
)
def test_compact_u32_truncated_at_every_width(data):
    with pytest.raises(WireDecodeError) as exc_info:
        decode_compact_u32(data)
    assert exc_info.value.reason == "truncated"


def test_compact_u32_exceeds_u32_checked_before_reading_payload():
    # mode 3, n = (byte0 >> 2) + 4 = 5 (byte0 = 0b0100_0011 = 0x43), only 1 byte total given --
    # "exceeds u32" must fire on the declared length alone, before attempting to read the (absent)
    # 5 payload bytes, so this is "exceeds u32", not "truncated".
    data = bytes([0x43])
    with pytest.raises(WireDecodeError) as exc_info:
        decode_compact_u32(data)
    assert exc_info.value.reason == "exceeds u32"


def test_compact_u32_trailing_bytes_after_full_value():
    data = encode_compact_u32(63) + b"\x00"
    with pytest.raises(WireDecodeError) as exc_info:
        decode_compact_u32(data)
    assert exc_info.value.reason == "trailing bytes"


@pytest.mark.parametrize("value", [0, 1, 63, 64, 16383, 16384, 2**30 - 1, 2**30, 2**32 - 1])
def test_compact_u32_encode_decode_round_trip(value):
    encoded = encode_compact_u32(value)
    assert decode_compact_u32(encoded) == value


@pytest.mark.parametrize("value", [-1, 2**32, "0", 1.5, True])
def test_encode_compact_u32_rejects_out_of_range_or_wrong_type(value):
    with pytest.raises(ValueError):
        encode_compact_u32(value)


# 2. Reader: fixed-width truncation, bool's full rejection space, finish() ------------------------


@pytest.mark.parametrize(
    "width_name,n", [("u8", 1), ("u16", 2), ("u32", 4), ("u64", 8), ("u128", 16)]
)
def test_reader_fixed_width_truncated(width_name, n):
    reader = Reader(b"\xff" * (n - 1))
    method = getattr(reader, width_name)
    with pytest.raises(WireDecodeError) as exc_info:
        method()
    assert exc_info.value.reason == "truncated"


def test_reader_fixed_width_exact_values():
    assert Reader(b"\x2a").u8() == 0x2A
    assert Reader(b"\x01\x00").u16() == 1
    assert Reader((2**32 - 1).to_bytes(4, "little")).u32() == 2**32 - 1
    assert Reader((2**64 - 1).to_bytes(8, "little")).u64() == 2**64 - 1
    assert Reader((2**128 - 1).to_bytes(16, "little")).u128() == 2**128 - 1


def test_reader_bool_accepts_only_00_and_01():
    assert Reader(b"\x00").bool_() is False
    assert Reader(b"\x01").bool_() is True


@pytest.mark.parametrize("byte", list(range(0x02, 0x100)))
def test_reader_bool_rejects_every_other_byte(byte):
    with pytest.raises(WireDecodeError) as exc_info:
        Reader(bytes([byte])).bool_()
    assert exc_info.value.reason == "invalid bool"


def test_reader_bool_truncated_on_empty():
    with pytest.raises(WireDecodeError) as exc_info:
        Reader(b"").bool_()
    assert exc_info.value.reason == "truncated"


def test_reader_h256_truncated_when_short():
    with pytest.raises(WireDecodeError) as exc_info:
        Reader(b"\x00" * 31).h256()
    assert exc_info.value.reason == "truncated"


def test_reader_finish_raises_on_trailing_bytes():
    reader = Reader(b"\x01\x02")
    reader.take(1)
    with pytest.raises(WireDecodeError) as exc_info:
        reader.finish()
    assert exc_info.value.reason == "trailing bytes"


def test_reader_finish_is_a_no_op_when_fully_consumed():
    reader = Reader(b"\x01\x02")
    reader.take(2)
    reader.finish()  # must not raise


def test_reader_offset_and_remaining_advance_with_reads():
    reader = Reader(b"\x00" * 10)
    assert reader.offset == 0
    assert reader.remaining() == 10
    reader.take(3)
    assert reader.offset == 3
    assert reader.remaining() == 7


def test_wire_decode_error_str_starts_with_reason():
    exc = WireDecodeError("truncated", 5, "need 3 bytes")
    assert str(exc).startswith("truncated")
    assert exc.reason == "truncated"
    assert exc.offset == 5
    assert exc.detail == "need 3 bytes"


# 3. Enum-tag rejection shapes ---------------------------------------------------------------------


def test_decode_leaf_version_rejects_tag_beyond_v3():
    with pytest.raises(WireDecodeError) as exc_info:
        objects.decode_leaf_version(Reader(bytes([4])))
    assert exc_info.value.reason == "unknown tag"


def test_decode_retention_class_rejects_tag_beyond_leased():
    with pytest.raises(WireDecodeError) as exc_info:
        objects.decode_retention_class(Reader(bytes([2])))
    assert exc_info.value.reason == "unknown tag"


def test_decode_tee_type_rejects_tag_beyond_simulator():
    with pytest.raises(WireDecodeError) as exc_info:
        objects.decode_tee_type(Reader(bytes([4])))
    assert exc_info.value.reason == "unknown tag"


def test_decode_policy_class_rejects_unknown_tag():
    with pytest.raises(WireDecodeError) as exc_info:
        objects.DecodePolicyClass.decode_from(Reader(bytes([5])))
    assert exc_info.value.reason == "unknown tag"


def test_output_transform_rejects_unknown_tag():
    with pytest.raises(WireDecodeError) as exc_info:
        objects.OutputTransform.decode_from(Reader(bytes([2])))
    assert exc_info.value.reason == "unknown tag"


# 4. VerifiedTurn: PathTooLong at decode -----------------------------------------------------


def test_verified_turn_decode_rejects_path_longer_than_64():
    fixed = (
        bytes([0])  # leaf_version = V0
        + (0).to_bytes(4, "little")  # turn_index
        + H256_A
        + H256_B
        + (0).to_bytes(16, "little")  # g_n
        + b"\x00" * 32  # decode_policy_hash (must be zero for V0)
        + b"\x00" * 32  # h_ids (must be zero for V0)
        + b"\x00" * 32  # toploc_commitment_hash (must be zero for V0)
        + (0).to_bytes(8, "little") * 3  # timing
        + b"\x55" * 64  # enclave_sig
    )
    path = encode_compact_u32(65) + (H256_C + b"\x00") * 65
    with pytest.raises(WireDecodeError) as exc_info:
        objects.VerifiedTurn.decode(fixed + path)
    assert exc_info.value.reason == "PathTooLong"


def test_verified_turn_decode_accepts_path_of_exactly_64():
    fixed = (
        bytes([0])
        + (0).to_bytes(4, "little")
        + H256_A
        + H256_B
        + (0).to_bytes(16, "little")
        + b"\x00" * 32
        + b"\x00" * 32
        + b"\x00" * 32
        + (0).to_bytes(8, "little") * 3
        + b"\x55" * 64
    )
    path = encode_compact_u32(64) + (H256_C + b"\x00") * 64
    turn = objects.VerifiedTurn.decode(fixed + path)
    assert len(turn.merkle_path) == 64


# 5. settle.verified_work_from_turns: TooManyTurns, AggregateGnOverflow -------------------------


def test_verified_work_from_turns_rejects_too_many_turns():
    turns = tuple(_turn(i) for i in range(3))
    with pytest.raises(WireDecodeError) as exc_info:
        settle.verified_work_from_turns(
            turns,
            channel_id=CHANNEL_ID,
            final_root=b"\x00" * 32,
            channel_has_decode_policy=False,
            max_turns=2,
        )
    assert exc_info.value.reason == "TooManyTurns"


def test_verified_work_from_turns_rejects_aggregate_gn_overflow():
    max_gn = 2**128 - 1
    leaf0 = _turn(0, g_n=max_gn).leaf_hash(CHANNEL_ID)
    leaf1 = _turn(1, g_n=max_gn).leaf_hash(CHANNEL_ID)
    root = hashes.merkle_node(leaf0, leaf1)
    turn0 = _turn(0, g_n=max_gn, merkle_path=((leaf1, False),))
    turn1 = _turn(1, g_n=max_gn, merkle_path=((leaf0, True),))

    with pytest.raises(WireDecodeError) as exc_info:
        settle.verified_work_from_turns(
            [turn0, turn1],
            channel_id=CHANNEL_ID,
            final_root=root,
            channel_has_decode_policy=False,
        )
    assert exc_info.value.reason == "AggregateGnOverflow"


def test_verified_work_from_turns_accepts_two_distinct_turns_under_a_shared_root():
    leaf0 = _turn(0, g_n=1).leaf_hash(CHANNEL_ID)
    leaf1 = _turn(1, g_n=2).leaf_hash(CHANNEL_ID)
    root = hashes.merkle_node(leaf0, leaf1)
    turn0 = _turn(0, g_n=1, merkle_path=((leaf1, False),))
    turn1 = _turn(1, g_n=2, merkle_path=((leaf0, True),))

    result = settle.verified_work_from_turns(
        [turn0, turn1], channel_id=CHANNEL_ID, final_root=root, channel_has_decode_policy=False
    )
    assert result.turn_count == 2
    assert result.aggregate_gn == 3
    assert result.leaf_hashes == (leaf0, leaf1)
    assert all(sig.status == "not_verified" for sig in result.signatures)


def test_verified_work_from_turns_rejects_leaf_not_in_root():
    turn0 = _turn(0, g_n=1, merkle_path=())
    with pytest.raises(WireDecodeError) as exc_info:
        settle.verified_work_from_turns(
            [turn0], channel_id=CHANNEL_ID, final_root=b"\xff" * 32, channel_has_decode_policy=False
        )
    assert exc_info.value.reason == "LeafNotInRoot"


def test_verified_work_from_turns_rejects_duplicate_turn_index():
    leaf = _turn(0, g_n=1).leaf_hash(CHANNEL_ID)
    turn_a = _turn(0, g_n=1, merkle_path=())
    turn_b = _turn(0, g_n=1, merkle_path=())
    with pytest.raises(WireDecodeError) as exc_info:
        settle.verified_work_from_turns(
            [turn_a, turn_b],
            channel_id=CHANNEL_ID,
            final_root=leaf,
            channel_has_decode_policy=False,
        )
    assert exc_info.value.reason == "DuplicateVerifiedTurn"


def test_verified_work_from_turns_rejects_path_too_long_before_walking():
    long_path = tuple((H256_C, False) for _ in range(65))
    turn0 = _turn(0, g_n=1, merkle_path=long_path)
    with pytest.raises(WireDecodeError) as exc_info:
        settle.verified_work_from_turns(
            [turn0], channel_id=CHANNEL_ID, final_root=b"\x00" * 32, channel_has_decode_policy=False
        )
    assert exc_info.value.reason == "PathTooLong"


def test_verified_work_from_turns_unsupported_leaf_version_checked_before_path_walk():
    turn0 = _turn(0, leaf_version=objects.LeafVersion.V0, g_n=1, merkle_path=())
    with pytest.raises(WireDecodeError) as exc_info:
        settle.verified_work_from_turns(
            [turn0],
            channel_id=CHANNEL_ID,
            final_root=b"\x00" * 32,  # deliberately wrong root: would also be LeafNotInRoot
            channel_has_decode_policy=True,
        )
    assert exc_info.value.reason == "UnsupportedLeafVersion"


# 6. merkle_root_of_leaves on 0/1/2/3 leaves -------------------------------------------------


def test_merkle_root_of_leaves_empty_is_32_zero_bytes():
    assert hashes.merkle_root_of_leaves([]) == b"\x00" * 32


def test_merkle_root_of_leaves_single_leaf_is_the_leaf():
    assert hashes.merkle_root_of_leaves([H256_A]) == H256_A


def test_merkle_root_of_leaves_two_leaves_is_one_node():
    assert hashes.merkle_root_of_leaves([H256_A, H256_B]) == hashes.merkle_node(H256_A, H256_B)


def test_merkle_root_of_leaves_three_leaves_duplicates_the_last():
    # (A, B) -> node_ab; (C, C) -> node_cc; root = node(node_ab, node_cc)
    node_ab = hashes.merkle_node(H256_A, H256_B)
    node_cc = hashes.merkle_node(H256_C, H256_C)
    expected = hashes.merkle_node(node_ab, node_cc)
    assert hashes.merkle_root_of_leaves([H256_A, H256_B, H256_C]) == expected


def test_merkle_root_of_leaves_four_leaves_no_duplication_needed():
    node_ab = hashes.merkle_node(H256_A, H256_B)
    node_cd = hashes.merkle_node(H256_C, H256_D)
    expected = hashes.merkle_node(node_ab, node_cd)
    assert hashes.merkle_root_of_leaves([H256_A, H256_B, H256_C, H256_D]) == expected


# 7. DataRef / DecodePolicy encode/decode round-trips ----------------------------------------------


def test_data_ref_round_trip_ephemeral():
    original = objects.DataRef(
        commitment=H256_A, provider_id=7, retention=objects.RetentionClass.EPHEMERAL
    )
    assert objects.DataRef.decode(original.encode()) == original


def test_data_ref_round_trip_leased():
    original = objects.DataRef(
        commitment=H256_A, provider_id=255, retention=objects.RetentionClass.LEASED
    )
    assert objects.DataRef.decode(original.encode()) == original


def test_decode_policy_round_trip_other_class():
    policy = objects.DecodePolicy(
        version=1,
        policy_class=objects.DecodePolicyClass.other(0x1234),
        tokenizer_hash=H256_A,
        sampling=objects.SamplingParams(
            temperature_milli=100, top_p_ppm=900_000, top_k=40,
            repetition_penalty_ppm=1_100_000, beam_width=2, seed=7,
        ),
        stop_conditions_hash=H256_B,
        output_transform=objects.OutputTransform.identity(),
        class_policy_hash=H256_C,
    )
    encoded = policy.encode()
    # 126 default, with the 1-byte bare class tag replaced by a 3-byte Other(u16) tag+payload.
    assert len(encoded) == 128
    assert objects.DecodePolicy.decode(encoded) == policy


def test_decode_policy_round_trip_transform_id():
    policy = objects.DecodePolicy(
        version=2,
        policy_class=objects.DecodePolicyClass.rollout(),
        tokenizer_hash=H256_A,
        sampling=objects.SamplingParams(
            temperature_milli=0, top_p_ppm=1_000_000, top_k=0,
            repetition_penalty_ppm=1_000_000, beam_width=1, seed=0,
        ),
        stop_conditions_hash=H256_B,
        output_transform=objects.OutputTransform.transform(H256_D),
        class_policy_hash=H256_C,
    )
    encoded = policy.encode()
    assert len(encoded) == 126 + 32  # TransformId(H256) carries 32 extra bytes
    assert objects.DecodePolicy.decode(encoded) == policy


def test_decode_policy_class_constructors_reject_mismatched_other_value():
    with pytest.raises(ValueError):
        objects.DecodePolicyClass(tag=0, other_value=5)  # non-Other tag must not carry a value
    with pytest.raises(ValueError):
        objects.DecodePolicyClass(tag=4)  # Other tag requires a value


def test_output_transform_rejects_mismatched_transform_id():
    with pytest.raises(ValueError):
        objects.OutputTransform(tag=0, transform_id=H256_A)
    with pytest.raises(ValueError):
        objects.OutputTransform(tag=1)


# 8. verify.py: NoVerifier and StubVerifier structural checks -------------------------------------


def test_no_verifier_and_none_are_treated_identically():
    leaf_hash = H256_A
    pubkey = H256_B
    sig = b"\x00" * 64
    assert check_leaf_signature(leaf_hash, pubkey, sig, None).status == "not_verified"
    assert check_leaf_signature(leaf_hash, pubkey, sig, NoVerifier()).status == "not_verified"


def test_check_leaf_signature_rejects_wrong_length_key_or_signature():
    with pytest.raises(ValueError):
        check_leaf_signature(H256_A, b"\x00" * 31, b"\x00" * 64)
    with pytest.raises(ValueError):
        check_leaf_signature(H256_A, b"\x00" * 32, b"\x00" * 63)


def test_stub_verifier_rejects_everything_when_empty():
    stub = StubVerifier(())
    assert stub.verify(H256_A, H256_B, b"\x00" * 64) is False
