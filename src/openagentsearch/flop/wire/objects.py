"""Typed, frozen dataclasses for the FLOP v1 wire objects and their SCALE decoders: `DataRef`,
`DecodePolicy` (+ `SamplingParams`, `DecodePolicyClass`, `OutputTransform`), `TeeType`,
`ValidatorAttestation`, `LeafVersion` + `VerifiedTurn`, `ReceiptV1` + `LegacyReceipt`, and the FCC4
DA transcript container (`Fcc4Turn` + `Fcc4Transcript`).

Every top-level object has a `decode(data: bytes) -> T` classmethod that consumes `data` exactly
(calls `Reader.finish()`) and, where nesting is needed, a `decode_from(reader: Reader) -> T`
variant that does not. Every decoder is fail-closed: unknown enum tags, inconsistent fields, wrong
lengths and leftover bytes all raise `WireDecodeError` with one of the exact reasons documented in
`scale.WireDecodeError`, never a silent best-effort result.

NOT guaranteed: decoding an object successfully is NOT the same as it being valid FLOP chain state
-- `channel_has_decode_policy`, quorum, signer-activity and similar channel-state facts are not
wire bytes and are not checked here (see `settle.py` and `verify.py`, which take them as explicit
caller-supplied inputs). No object in this module talks to a chain, a wallet, or the network.
"""

from dataclasses import dataclass
from enum import IntEnum

from .hashes import (
    decode_policy_hash_v1,
    leaf_hash as _leaf_hash_fn,
    legacy_receipt_message,
    receipt_message_v1,
    report_data_v1,
    report_data_v1_preimage,
)
from .scale import Reader, WireDecodeError

_U64_MAX = 2**64 - 1
_U128_MAX = 2**128 - 1
_ZERO_H256 = b"\x00" * 32
_MAX_MERKLE_PATH_LEN = 64


def _require_h256(name: str, value: bytes) -> None:
    if not isinstance(value, bytes) or len(value) != 32:
        raise ValueError(f"{name} must be exactly 32 bytes (H256), got {value!r}")


def _require_bytes_len(name: str, value: bytes, length: int) -> None:
    if not isinstance(value, bytes) or len(value) != length:
        raise ValueError(f"{name} must be exactly {length} bytes, got {value!r}")


def _require_uint(name: str, value: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an int, got {value!r}")
    if value < 0 or value > maximum:
        raise ValueError(f"{name} must be in [0, {maximum}], got {value!r}")


def _require_bool(name: str, value: bool) -> None:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a bool, got {value!r}")


# ============================================================================================
# DataRef v1 (F.4; 34 B: commitment H256 || provider_id u8 || retention_class enum tag)
# ============================================================================================


class RetentionClass(IntEnum):
    """`DataRef.retention_class` SCALE enum tag. Only `EPHEMERAL` has a corpus vector; `LEASED` is
    named in the yellow paper text with no positive corpus example (see `DataRef` docstring)."""

    EPHEMERAL = 0
    LEASED = 1


def decode_retention_class(reader: Reader) -> RetentionClass:
    """Read one tag byte and resolve it to a `RetentionClass`. Any tag other than 0/1 raises
    `WireDecodeError("unknown tag", ...)` -- exercised directly (not through a full `DataRef`) by
    the corpus's `unknown_retention_enum` negative case (`bytes_hex = "02"`)."""
    start = reader.offset
    tag = reader.u8()
    try:
        return RetentionClass(tag)
    except ValueError:
        raise WireDecodeError(
            "unknown tag", start, f"DataRef.retention_class tag={tag}"
        ) from None


@dataclass(frozen=True)
class DataRef:
    """`commitment`(H256) || `provider_id`(plain `u8`, NOT `Compact<u32>` -- the yellow paper types
    it as a fixed byte; a `Compact<u32>` decoder would misinterpret `provider_id`'s low bits as a
    SCALE mode selector, which is wrong here) || `retention`(`RetentionClass` tag) = 34 B.

    NOT guaranteed: an unknown `provider_id` value decodes successfully here (it is a plain byte,
    0-255) -- whether it resolves to a real registered provider is a registry lookup this module
    does not perform."""

    commitment: bytes
    provider_id: int
    retention: RetentionClass

    def __post_init__(self) -> None:
        _require_h256("commitment", self.commitment)
        _require_uint("provider_id", self.provider_id, 255)
        if not isinstance(self.retention, RetentionClass):
            raise ValueError(f"retention must be a RetentionClass, got {self.retention!r}")

    @classmethod
    def decode_from(cls, reader: Reader) -> "DataRef":
        commitment = reader.h256()
        provider_id = reader.u8()
        retention = decode_retention_class(reader)
        return cls(commitment=commitment, provider_id=provider_id, retention=retention)

    @classmethod
    def decode(cls, data: bytes) -> "DataRef":
        reader = Reader(data)
        obj = cls.decode_from(reader)
        reader.finish()
        return obj

    def encode(self) -> bytes:
        """34 B: `commitment || provider_id || retention_tag`. The only positive vector the corpus
        has is `retention=Ephemeral`; a `Leased` `DataRef` built and encoded by this method is not
        corpus-covered (documented, tested as a round-trip only)."""
        return self.commitment + bytes([self.provider_id]) + bytes([int(self.retention)])


# ============================================================================================
# DecodePolicy v1 (F.1; SamplingParams 26 B; DecodePolicy variable, 126 B for the corpus default)
# ============================================================================================


@dataclass(frozen=True)
class SamplingParams:
    """26 B, fixed-width LE, no compact framing, no floats: `temperature_milli:u32`,
    `top_p_ppm:u32`, `top_k:u32`, `repetition_penalty_ppm:u32`, `beam_width:u16`, `seed:u64`."""

    temperature_milli: int
    top_p_ppm: int
    top_k: int
    repetition_penalty_ppm: int
    beam_width: int
    seed: int

    def __post_init__(self) -> None:
        _require_uint("temperature_milli", self.temperature_milli, 2**32 - 1)
        _require_uint("top_p_ppm", self.top_p_ppm, 2**32 - 1)
        _require_uint("top_k", self.top_k, 2**32 - 1)
        _require_uint("repetition_penalty_ppm", self.repetition_penalty_ppm, 2**32 - 1)
        _require_uint("beam_width", self.beam_width, 2**16 - 1)
        _require_uint("seed", self.seed, _U64_MAX)

    @classmethod
    def decode_from(cls, reader: Reader) -> "SamplingParams":
        return cls(
            temperature_milli=reader.u32(),
            top_p_ppm=reader.u32(),
            top_k=reader.u32(),
            repetition_penalty_ppm=reader.u32(),
            beam_width=reader.u16(),
            seed=reader.u64(),
        )

    @classmethod
    def decode(cls, data: bytes) -> "SamplingParams":
        reader = Reader(data)
        obj = cls.decode_from(reader)
        reader.finish()
        return obj

    def encode(self) -> bytes:
        return (
            self.temperature_milli.to_bytes(4, "little")
            + self.top_p_ppm.to_bytes(4, "little")
            + self.top_k.to_bytes(4, "little")
            + self.repetition_penalty_ppm.to_bytes(4, "little")
            + self.beam_width.to_bytes(2, "little")
            + self.seed.to_bytes(8, "little")
        )


_CLASS_TEXT_GENERATION = 0
_CLASS_IMAGE_DENOISE = 1
_CLASS_ROLLOUT = 2
_CLASS_CONTROL_LOOP = 3
_CLASS_OTHER = 4


@dataclass(frozen=True)
class DecodePolicyClass:
    """`DecodePolicy.class` SCALE enum: `TextGeneration=0, ImageDenoise=1, Rollout=2,
    ControlLoop=3` are bare (1 B total); `Other(u16)=4` carries 2 more bytes (`other_value`, 3 B
    total). Use the named constructors (`text_generation()`, ..., `other(value)`) rather than the
    constructor directly -- `other_value` must be `None` for every tag except 4, and non-`None`
    for tag 4, which `__post_init__` enforces either way."""

    tag: int
    other_value: int | None = None

    def __post_init__(self) -> None:
        _require_uint("tag", self.tag, _CLASS_OTHER)
        if self.tag == _CLASS_OTHER:
            if self.other_value is None:
                raise ValueError("other_value is required when tag == Other (4)")
            _require_uint("other_value", self.other_value, 2**16 - 1)
        elif self.other_value is not None:
            raise ValueError("other_value must be None unless tag == Other (4)")

    @classmethod
    def text_generation(cls) -> "DecodePolicyClass":
        return cls(tag=_CLASS_TEXT_GENERATION)

    @classmethod
    def image_denoise(cls) -> "DecodePolicyClass":
        return cls(tag=_CLASS_IMAGE_DENOISE)

    @classmethod
    def rollout(cls) -> "DecodePolicyClass":
        return cls(tag=_CLASS_ROLLOUT)

    @classmethod
    def control_loop(cls) -> "DecodePolicyClass":
        return cls(tag=_CLASS_CONTROL_LOOP)

    @classmethod
    def other(cls, value: int) -> "DecodePolicyClass":
        return cls(tag=_CLASS_OTHER, other_value=value)

    @classmethod
    def decode_from(cls, reader: Reader) -> "DecodePolicyClass":
        start = reader.offset
        tag = reader.u8()
        if tag == _CLASS_OTHER:
            return cls(tag=tag, other_value=reader.u16())
        bare_tags = (
            _CLASS_TEXT_GENERATION, _CLASS_IMAGE_DENOISE, _CLASS_ROLLOUT, _CLASS_CONTROL_LOOP
        )
        if tag in bare_tags:
            return cls(tag=tag)
        raise WireDecodeError("unknown tag", start, f"DecodePolicy.class tag={tag}")

    def encode(self) -> bytes:
        if self.tag == _CLASS_OTHER:
            assert self.other_value is not None
            return bytes([self.tag]) + self.other_value.to_bytes(2, "little")
        return bytes([self.tag])


_TRANSFORM_IDENTITY = 0
_TRANSFORM_ID = 1


@dataclass(frozen=True)
class OutputTransform:
    """`DecodePolicy.output_transform` SCALE enum: `Identity=0` bare (1 B); `TransformId(H256)=1`
    carries a 32-byte hash (33 B total)."""

    tag: int
    transform_id: bytes | None = None

    def __post_init__(self) -> None:
        _require_uint("tag", self.tag, _TRANSFORM_ID)
        if self.tag == _TRANSFORM_ID:
            if self.transform_id is None:
                raise ValueError("transform_id is required when tag == TransformId (1)")
            _require_h256("transform_id", self.transform_id)
        elif self.transform_id is not None:
            raise ValueError("transform_id must be None unless tag == TransformId (1)")

    @classmethod
    def identity(cls) -> "OutputTransform":
        return cls(tag=_TRANSFORM_IDENTITY)

    @classmethod
    def transform(cls, value: bytes) -> "OutputTransform":
        return cls(tag=_TRANSFORM_ID, transform_id=value)

    @classmethod
    def decode_from(cls, reader: Reader) -> "OutputTransform":
        start = reader.offset
        tag = reader.u8()
        if tag == _TRANSFORM_IDENTITY:
            return cls(tag=tag)
        if tag == _TRANSFORM_ID:
            return cls(tag=tag, transform_id=reader.h256())
        raise WireDecodeError("unknown tag", start, f"DecodePolicy.output_transform tag={tag}")

    def encode(self) -> bytes:
        if self.tag == _TRANSFORM_ID:
            assert self.transform_id is not None
            return bytes([self.tag]) + self.transform_id
        return bytes([self.tag])


@dataclass(frozen=True)
class DecodePolicy:
    """`version:u16` || `class`(`DecodePolicyClass`) || `tokenizer_hash`(H256) ||
    `sampling`(`SamplingParams`, 26 B) || `stop_conditions_hash`(H256) ||
    `output_transform`(`OutputTransform`) || `class_policy_hash`(H256). For the corpus's
    `DecodePolicy::default()` vector (`class=TextGeneration`, `output_transform=Identity`) this is
    exactly 126 B; a non-default `class=Other(u16)` or `output_transform=TransformId(H256)` makes
    `encode()` longer (127 B / 158 B respectively) -- 126 B is the corpus's example length, not a
    universal constant."""

    version: int
    policy_class: DecodePolicyClass
    tokenizer_hash: bytes
    sampling: SamplingParams
    stop_conditions_hash: bytes
    output_transform: OutputTransform
    class_policy_hash: bytes

    def __post_init__(self) -> None:
        _require_uint("version", self.version, 2**16 - 1)
        if not isinstance(self.policy_class, DecodePolicyClass):
            raise ValueError(f"policy_class must be a DecodePolicyClass, got {self.policy_class!r}")
        _require_h256("tokenizer_hash", self.tokenizer_hash)
        if not isinstance(self.sampling, SamplingParams):
            raise ValueError(f"sampling must be a SamplingParams, got {self.sampling!r}")
        _require_h256("stop_conditions_hash", self.stop_conditions_hash)
        if not isinstance(self.output_transform, OutputTransform):
            raise ValueError(
                f"output_transform must be an OutputTransform, got {self.output_transform!r}"
            )
        _require_h256("class_policy_hash", self.class_policy_hash)

    @classmethod
    def decode_from(cls, reader: Reader) -> "DecodePolicy":
        version = reader.u16()
        policy_class = DecodePolicyClass.decode_from(reader)
        tokenizer_hash = reader.h256()
        sampling = SamplingParams.decode_from(reader)
        stop_conditions_hash = reader.h256()
        output_transform = OutputTransform.decode_from(reader)
        class_policy_hash = reader.h256()
        return cls(
            version=version,
            policy_class=policy_class,
            tokenizer_hash=tokenizer_hash,
            sampling=sampling,
            stop_conditions_hash=stop_conditions_hash,
            output_transform=output_transform,
            class_policy_hash=class_policy_hash,
        )

    @classmethod
    def decode(cls, data: bytes) -> "DecodePolicy":
        reader = Reader(data)
        obj = cls.decode_from(reader)
        reader.finish()
        return obj

    def encode(self) -> bytes:
        return (
            self.version.to_bytes(2, "little")
            + self.policy_class.encode()
            + self.tokenizer_hash
            + self.sampling.encode()
            + self.stop_conditions_hash
            + self.output_transform.encode()
            + self.class_policy_hash
        )

    def policy_hash(self) -> bytes:
        """`decode_policy_hash_v1(self.encode())` -- SHA256 over
        `"FLOP_DECODE_POLICY_HASH_V1" || self.encode()`."""
        return decode_policy_hash_v1(self.encode())


# ============================================================================================
# TeeType (shared by ValidatorAttestation and report_data)
# ============================================================================================


class TeeType(IntEnum):
    INTEL_TDX = 0
    NVIDIA_HOPPER_CC = 1
    NVIDIA_RUBIN_CC = 2
    SIMULATOR = 3


def decode_tee_type(reader: Reader) -> TeeType:
    """Read one tag byte and resolve it to a `TeeType`. Any tag other than 0-3 raises
    `WireDecodeError("unknown tag", ...)`."""
    start = reader.offset
    tag = reader.u8()
    try:
        return TeeType(tag)
    except ValueError:
        raise WireDecodeError("unknown tag", start, f"TeeType tag={tag}") from None


# ============================================================================================
# ValidatorAttestation v1 (F.2; 275 B full; 179 B signed subset)
# ============================================================================================


@dataclass(frozen=True)
class ValidatorAttestation:
    """The 10-field signed subset (`task_hash` through `hardware_id_hash`, 179 B) plus
    `validator_id`(32 B, NOT signed) and `signature`(64 B, NOT signed -- it IS the signature) =
    275 B total, corpus-verified against `direct_rail_v1.validator_attestation_scale_hex` /
    `_signable_hex`."""

    task_hash: bytes
    gn_weight: int
    latency_ms: int
    model_hash: bytes
    output_hash: bytes
    decode_policy_hash: bytes
    tee_type: TeeType
    quote_verified: bool
    event_log_verified: bool
    hardware_id_hash: bytes
    validator_id: bytes
    signature: bytes

    def __post_init__(self) -> None:
        _require_h256("task_hash", self.task_hash)
        _require_uint("gn_weight", self.gn_weight, _U64_MAX)
        _require_uint("latency_ms", self.latency_ms, _U64_MAX)
        _require_h256("model_hash", self.model_hash)
        _require_h256("output_hash", self.output_hash)
        _require_h256("decode_policy_hash", self.decode_policy_hash)
        if not isinstance(self.tee_type, TeeType):
            raise ValueError(f"tee_type must be a TeeType, got {self.tee_type!r}")
        _require_bool("quote_verified", self.quote_verified)
        _require_bool("event_log_verified", self.event_log_verified)
        _require_h256("hardware_id_hash", self.hardware_id_hash)
        _require_h256("validator_id", self.validator_id)
        _require_bytes_len("signature", self.signature, 64)

    @classmethod
    def decode_from(cls, reader: Reader) -> "ValidatorAttestation":
        task_hash = reader.h256()
        gn_weight = reader.u64()
        latency_ms = reader.u64()
        model_hash = reader.h256()
        output_hash = reader.h256()
        decode_policy_hash = reader.h256()
        tee_type = decode_tee_type(reader)
        quote_verified = reader.bool_()
        event_log_verified = reader.bool_()
        hardware_id_hash = reader.h256()
        validator_id = reader.h256()
        signature = reader.take(64)
        return cls(
            task_hash=task_hash,
            gn_weight=gn_weight,
            latency_ms=latency_ms,
            model_hash=model_hash,
            output_hash=output_hash,
            decode_policy_hash=decode_policy_hash,
            tee_type=tee_type,
            quote_verified=quote_verified,
            event_log_verified=event_log_verified,
            hardware_id_hash=hardware_id_hash,
            validator_id=validator_id,
            signature=signature,
        )

    @classmethod
    def decode(cls, data: bytes) -> "ValidatorAttestation":
        reader = Reader(data)
        obj = cls.decode_from(reader)
        reader.finish()
        return obj

    def signable_bytes(self) -> bytes:
        """179 B: the `report_data` preimage (145 B) || `quote_verified`(1) ||
        `event_log_verified`(1) || `hardware_id_hash`(32) -- the bytes sr25519 signs directly (no
        hash-first step)."""
        preimage = report_data_v1_preimage(
            self.task_hash,
            self.gn_weight,
            self.latency_ms,
            self.model_hash,
            self.output_hash,
            self.decode_policy_hash,
            int(self.tee_type),
        )
        return (
            preimage
            + bytes([1 if self.quote_verified else 0])
            + bytes([1 if self.event_log_verified else 0])
            + self.hardware_id_hash
        )

    def report_data(self) -> bytes:
        """64 B: `sha256(report_data_preimage)`(32) || `0x00 * 32`."""
        return report_data_v1(
            self.task_hash,
            self.gn_weight,
            self.latency_ms,
            self.model_hash,
            self.output_hash,
            self.decode_policy_hash,
            int(self.tee_type),
        )


# ============================================================================================
# VerifiedTurn (F.3; leaf_version + 268 B fixed fields + Vec<(H256,bool)> merkle_path)
# ============================================================================================


class LeafVersion(IntEnum):
    V0 = 0
    V1 = 1
    V2 = 2
    V3 = 3


def decode_leaf_version(reader: Reader) -> LeafVersion:
    """Read one tag byte and resolve it to a `LeafVersion`. Any tag other than 0-3 raises
    `WireDecodeError("unknown tag", ...)` -- exercised directly (not through a full `VerifiedTurn`)
    by the corpus's `unknown_leaf_enum` negative case (`bytes_hex = "04"`)."""
    start = reader.offset
    tag = reader.u8()
    try:
        return LeafVersion(tag)
    except ValueError:
        raise WireDecodeError(
            "unknown tag", start, f"VerifiedTurn.leaf_version tag={tag}"
        ) from None


@dataclass(frozen=True)
class VerifiedTurn:
    """The wire-submitted struct for `settle`/`respond_dispute`: a fixed 269-byte record (fields 1
    through 12) followed by `merkle_path: Vec<(H256, bool)>`. It always carries `h_ids` and
    `toploc_commitment_hash` on the wire regardless of `leaf_version` -- `leaf_version` selects
    which subset of these already-decoded fields feeds the leaf-hash *preimage*
    (`hashes.leaf_hash`), not which fields are present on the wire.

    Self-consistency, enforced by `decode_from`/`decode` (NOT by `__post_init__` -- constructing a
    `VerifiedTurn` directly with inconsistent fields is not itself rejected; only decoding
    untrusted wire bytes is): for `leaf_version` V0/V1, `decode_policy_hash`, `h_ids` and
    `toploc_commitment_hash` must all be zero; for V2, `h_ids` and `toploc_commitment_hash` must be
    zero (`decode_policy_hash` may be set); V3 has no zero-field requirement. Violating this raises
    `WireDecodeError("LeafFieldsInconsistent", ...)`. `merkle_path` longer than 64 entries raises
    `WireDecodeError("PathTooLong", ...)`.
    """

    leaf_version: LeafVersion
    turn_index: int
    h_in: bytes
    h_out: bytes
    g_n: int
    decode_policy_hash: bytes
    h_ids: bytes
    toploc_commitment_hash: bytes
    miner_recv_ms: int
    miner_done_ms: int
    latency_ms: int
    enclave_sig: bytes
    merkle_path: tuple[tuple[bytes, bool], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.leaf_version, LeafVersion):
            raise ValueError(f"leaf_version must be a LeafVersion, got {self.leaf_version!r}")
        _require_uint("turn_index", self.turn_index, 2**32 - 1)
        _require_h256("h_in", self.h_in)
        _require_h256("h_out", self.h_out)
        _require_uint("g_n", self.g_n, _U128_MAX)
        _require_h256("decode_policy_hash", self.decode_policy_hash)
        _require_h256("h_ids", self.h_ids)
        _require_h256("toploc_commitment_hash", self.toploc_commitment_hash)
        _require_uint("miner_recv_ms", self.miner_recv_ms, _U64_MAX)
        _require_uint("miner_done_ms", self.miner_done_ms, _U64_MAX)
        _require_uint("latency_ms", self.latency_ms, _U64_MAX)
        _require_bytes_len("enclave_sig", self.enclave_sig, 64)
        if not isinstance(self.merkle_path, tuple):
            raise ValueError("merkle_path must be a tuple of (H256, bool) pairs")
        for index, entry in enumerate(self.merkle_path):
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise ValueError(
                    f"merkle_path[{index}] must be a (bytes, bool) pair, got {entry!r}"
                )
            _require_h256(f"merkle_path[{index}][0]", entry[0])
            _require_bool(f"merkle_path[{index}][1]", entry[1])

    @classmethod
    def decode_from(cls, reader: Reader) -> "VerifiedTurn":
        start = reader.offset
        leaf_version = decode_leaf_version(reader)
        turn_index = reader.u32()
        h_in = reader.h256()
        h_out = reader.h256()
        g_n = reader.u128()
        decode_policy_hash = reader.h256()
        h_ids = reader.h256()
        toploc_commitment_hash = reader.h256()
        miner_recv_ms = reader.u64()
        miner_done_ms = reader.u64()
        latency_ms = reader.u64()
        enclave_sig = reader.take(64)

        path_len_offset = reader.offset
        path_len = reader.compact_u32()
        if path_len > _MAX_MERKLE_PATH_LEN:
            raise WireDecodeError(
                "PathTooLong",
                path_len_offset,
                f"merkle_path length {path_len} > {_MAX_MERKLE_PATH_LEN}",
            )
        path: list[tuple[bytes, bool]] = []
        for _ in range(path_len):
            sibling = reader.h256()
            sibling_is_left = reader.bool_()
            path.append((sibling, sibling_is_left))

        if leaf_version in (LeafVersion.V0, LeafVersion.V1):
            if (
                decode_policy_hash != _ZERO_H256
                or h_ids != _ZERO_H256
                or toploc_commitment_hash != _ZERO_H256
            ):
                raise WireDecodeError(
                    "LeafFieldsInconsistent", start, f"leaf_version={leaf_version.name}"
                )
        elif leaf_version == LeafVersion.V2:
            if h_ids != _ZERO_H256 or toploc_commitment_hash != _ZERO_H256:
                raise WireDecodeError(
                    "LeafFieldsInconsistent", start, f"leaf_version={leaf_version.name}"
                )

        return cls(
            leaf_version=leaf_version,
            turn_index=turn_index,
            h_in=h_in,
            h_out=h_out,
            g_n=g_n,
            decode_policy_hash=decode_policy_hash,
            h_ids=h_ids,
            toploc_commitment_hash=toploc_commitment_hash,
            miner_recv_ms=miner_recv_ms,
            miner_done_ms=miner_done_ms,
            latency_ms=latency_ms,
            enclave_sig=enclave_sig,
            merkle_path=tuple(path),
        )

    @classmethod
    def decode(cls, data: bytes) -> "VerifiedTurn":
        reader = Reader(data)
        obj = cls.decode_from(reader)
        reader.finish()
        return obj

    def leaf_hash(self, channel_id: bytes) -> bytes:
        """`blake2_256` of the version-selected preimage (`hashes.leaf_preimage`)."""
        return _leaf_hash_fn(int(self.leaf_version), channel_id, self)


def decode_verified_turn_vec(data: bytes) -> tuple[VerifiedTurn, ...]:
    """`Vec<VerifiedTurn>`: `Compact<u32>(count)` || `count` SCALE-encoded `VerifiedTurn`s,
    consuming `data` exactly (`Reader.finish()`)."""
    reader = Reader(data)
    count = reader.compact_u32()
    turns = tuple(VerifiedTurn.decode_from(reader) for _ in range(count))
    reader.finish()
    return turns


# ============================================================================================
# Agent receipt v1 (F.1; 125 B v1 message; 96 B legacy message; both signed directly)
# ============================================================================================


@dataclass(frozen=True)
class ReceiptV1:
    """`channel_id`, `final_root`, `aggregate_gn` (u128), `payable` (u128) -- the fields
    `receipt_message_v1` assembles behind the `"FLOP/COMPUTE_CHANNEL/RECEIPT"` domain and version
    byte."""

    channel_id: bytes
    final_root: bytes
    aggregate_gn: int
    payable: int

    def __post_init__(self) -> None:
        _require_h256("channel_id", self.channel_id)
        _require_h256("final_root", self.final_root)
        _require_uint("aggregate_gn", self.aggregate_gn, _U128_MAX)
        _require_uint("payable", self.payable, _U128_MAX)

    def message(self) -> bytes:
        """125 B raw message sr25519 signs directly (no hash-first step)."""
        return receipt_message_v1(
            self.channel_id, self.final_root, self.aggregate_gn, self.payable
        )


@dataclass(frozen=True)
class LegacyReceipt:
    """The historical, untagged 96-byte receipt preimage: the same four fields as `ReceiptV1`,
    with the domain string and version byte stripped. Historical-profile-only."""

    channel_id: bytes
    final_root: bytes
    aggregate_gn: int
    payable: int

    def __post_init__(self) -> None:
        _require_h256("channel_id", self.channel_id)
        _require_h256("final_root", self.final_root)
        _require_uint("aggregate_gn", self.aggregate_gn, _U128_MAX)
        _require_uint("payable", self.payable, _U128_MAX)

    def message(self) -> bytes:
        """96 B raw message, no domain/version prefix."""
        return legacy_receipt_message(
            self.channel_id, self.final_root, self.aggregate_gn, self.payable
        )


_RECEIPT_V1_DOMAIN = b"FLOP/COMPUTE_CHANNEL/RECEIPT"
_RECEIPT_V1_TOTAL_LEN = 125 + 64
_LEGACY_RECEIPT_TOTAL_LEN = 96 + 64


def decode_receipt_with_signature(data: bytes) -> tuple[ReceiptV1 | LegacyReceipt, bytes]:
    """Decode a `message || sig64` blob: 189 B (125 B v1 message + 64 B signature) decodes as
    `ReceiptV1` (the domain string and version byte are checked; a wrong domain raises
    `WireDecodeError("unknown magic", ...)`); 160 B (96 B legacy message + 64 B signature) decodes
    as `LegacyReceipt`. Anything shorter than 189 B that is not exactly 160 B raises
    `WireDecodeError("truncated", ...)`; anything longer than 189 B raises
    `WireDecodeError("trailing bytes", ...)`."""
    length = len(data)
    if length == _RECEIPT_V1_TOTAL_LEN:
        message, signature = data[:125], data[125:]
        if message[:28] != _RECEIPT_V1_DOMAIN:
            raise WireDecodeError(
                "unknown magic", 0, f"expected {_RECEIPT_V1_DOMAIN!r} domain, got {message[:28]!r}"
            )
        if message[28:29] != b"\x01":
            raise WireDecodeError(
                "unknown magic", 28, f"expected version 0x01, got 0x{message[28]:02x}"
            )
        receipt_v1 = ReceiptV1(
            channel_id=message[29:61],
            final_root=message[61:93],
            aggregate_gn=int.from_bytes(message[93:109], "little"),
            payable=int.from_bytes(message[109:125], "little"),
        )
        return receipt_v1, signature
    if length == _LEGACY_RECEIPT_TOTAL_LEN:
        message, signature = data[:96], data[96:]
        legacy = LegacyReceipt(
            channel_id=message[0:32],
            final_root=message[32:64],
            aggregate_gn=int.from_bytes(message[64:80], "little"),
            payable=int.from_bytes(message[80:96], "little"),
        )
        return legacy, signature
    if length > _RECEIPT_V1_TOTAL_LEN:
        raise WireDecodeError(
            "trailing bytes",
            _RECEIPT_V1_TOTAL_LEN,
            f"expected {_LEGACY_RECEIPT_TOTAL_LEN} or {_RECEIPT_V1_TOTAL_LEN} bytes, got {length}",
        )
    raise WireDecodeError(
        "truncated",
        length,
        f"expected {_LEGACY_RECEIPT_TOTAL_LEN} or {_RECEIPT_V1_TOTAL_LEN} bytes, got {length}",
    )


# ============================================================================================
# FCC4 DA transcript container (F.3 SDK current practice; 40 B header + variable per-turn records)
# ============================================================================================

_FCC4_MAGIC = b"FCC4"


@dataclass(frozen=True)
class Fcc4Turn:
    """One per-turn record inside an FCC4 transcript blob. `leaf_version` is a plain `u8` here
    (NOT a `LeafVersion` SCALE enum tag with unknown-tag rejection -- unlike `VerifiedTurn`, an
    out-of-range `leaf_version` in FCC4 falls through to the general `FccFieldsInconsistent`
    consistency check, not a dedicated `"unknown tag"` reason; this is a deliberate, corpus-blind
    distinction the brief draws explicitly). `policy_hash` is `None` iff `has_policy` is `False`;
    `send_ms`/`receive_ms`/`agent_sig` are all `None` iff `has_ack` is `False` -- this
    presence-flag-byte pattern is distinct from SCALE's `Option<T>` (same `00`/`01` byte values,
    different construction; FCC4 never uses `Option<T>` itself)."""

    leaf_version: int
    turn_index: int
    h_in: bytes
    h_out: bytes
    g_n: int
    has_policy: bool
    policy_hash: bytes | None
    h_ids: bytes
    toploc_hash: bytes
    miner_recv_ms: int
    miner_done_ms: int
    latency_ms: int
    enclave_sig: bytes
    has_ack: bool
    send_ms: int | None
    receive_ms: int | None
    agent_sig: bytes | None

    def __post_init__(self) -> None:
        _require_uint("leaf_version", self.leaf_version, 255)
        _require_uint("turn_index", self.turn_index, 2**32 - 1)
        _require_h256("h_in", self.h_in)
        _require_h256("h_out", self.h_out)
        _require_uint("g_n", self.g_n, _U128_MAX)
        _require_bool("has_policy", self.has_policy)
        if self.has_policy:
            if self.policy_hash is None:
                raise ValueError("policy_hash is required when has_policy is True")
            _require_h256("policy_hash", self.policy_hash)
        elif self.policy_hash is not None:
            raise ValueError("policy_hash must be None when has_policy is False")
        _require_h256("h_ids", self.h_ids)
        _require_h256("toploc_hash", self.toploc_hash)
        _require_uint("miner_recv_ms", self.miner_recv_ms, _U64_MAX)
        _require_uint("miner_done_ms", self.miner_done_ms, _U64_MAX)
        _require_uint("latency_ms", self.latency_ms, _U64_MAX)
        _require_bytes_len("enclave_sig", self.enclave_sig, 64)
        _require_bool("has_ack", self.has_ack)
        if self.has_ack:
            if self.send_ms is None or self.receive_ms is None or self.agent_sig is None:
                raise ValueError("send_ms/receive_ms/agent_sig are required when has_ack is True")
            _require_uint("send_ms", self.send_ms, _U64_MAX)
            _require_uint("receive_ms", self.receive_ms, _U64_MAX)
            _require_bytes_len("agent_sig", self.agent_sig, 64)
        elif self.send_ms is not None or self.receive_ms is not None or self.agent_sig is not None:
            raise ValueError("send_ms/receive_ms/agent_sig must be None when has_ack is False")


def _decode_fcc4_turn(reader: Reader) -> Fcc4Turn:
    start = reader.offset
    leaf_version = reader.u8()
    turn_index = reader.u32()
    h_in = reader.h256()
    h_out = reader.h256()
    g_n = reader.u128()

    has_policy_offset = reader.offset
    has_policy_byte = reader.u8()
    if has_policy_byte not in (0, 1):
        raise WireDecodeError(
            "invalid bool", has_policy_offset, f"has_policy byte={has_policy_byte}"
        )
    has_policy = bool(has_policy_byte)
    policy_hash = reader.h256() if has_policy else None

    h_ids = reader.h256()
    toploc_hash = reader.h256()
    miner_recv_ms = reader.u64()
    miner_done_ms = reader.u64()
    latency_ms = reader.u64()
    enclave_sig = reader.take(64)

    has_ack_offset = reader.offset
    has_ack_byte = reader.u8()
    if has_ack_byte not in (0, 1):
        raise WireDecodeError("invalid bool", has_ack_offset, f"has_ack byte={has_ack_byte}")
    has_ack = bool(has_ack_byte)
    if has_ack:
        send_ms: int | None = reader.u64()
        receive_ms: int | None = reader.u64()
        agent_sig: bytes | None = reader.take(64)
    else:
        send_ms = receive_ms = agent_sig = None

    if leaf_version in (0, 1):
        consistent = not has_policy and h_ids == _ZERO_H256 and toploc_hash == _ZERO_H256
    elif leaf_version == 2:
        consistent = has_policy and h_ids == _ZERO_H256 and toploc_hash == _ZERO_H256
    elif leaf_version == 3:
        consistent = has_policy and h_ids != _ZERO_H256
    else:
        consistent = False
    if not consistent:
        raise WireDecodeError("FccFieldsInconsistent", start, f"leaf_version={leaf_version}")

    return Fcc4Turn(
        leaf_version=leaf_version,
        turn_index=turn_index,
        h_in=h_in,
        h_out=h_out,
        g_n=g_n,
        has_policy=has_policy,
        policy_hash=policy_hash,
        h_ids=h_ids,
        toploc_hash=toploc_hash,
        miner_recv_ms=miner_recv_ms,
        miner_done_ms=miner_done_ms,
        latency_ms=latency_ms,
        enclave_sig=enclave_sig,
        has_ack=has_ack,
        send_ms=send_ms,
        receive_ms=receive_ms,
        agent_sig=agent_sig,
    )


@dataclass(frozen=True)
class Fcc4Transcript:
    """`"FCC4"`(4) magic || `channel_id`(H256) || `turn_count:u32LE`(4) || `turn_count` per-turn
    records (`Fcc4Turn`). `turn_count` is authoritative: decoding stops after exactly that many
    records, and any byte left over (`Reader.finish()`) is rejected as `"trailing bytes"`; running
    out of bytes mid-record is `"truncated"`; a magic other than `"FCC4"` is
    `"unknown magic"`."""

    channel_id: bytes
    turns: tuple[Fcc4Turn, ...]

    def __post_init__(self) -> None:
        _require_h256("channel_id", self.channel_id)
        if not isinstance(self.turns, tuple):
            raise ValueError("turns must be a tuple of Fcc4Turn")

    @classmethod
    def decode(cls, data: bytes) -> "Fcc4Transcript":
        reader = Reader(data)
        magic = reader.take(4)
        if magic != _FCC4_MAGIC:
            raise WireDecodeError("unknown magic", 0, f"expected {_FCC4_MAGIC!r}, got {magic!r}")
        channel_id = reader.h256()
        turn_count = reader.u32()
        turns = tuple(_decode_fcc4_turn(reader) for _ in range(turn_count))
        reader.finish()
        return cls(channel_id=channel_id, turns=turns)
