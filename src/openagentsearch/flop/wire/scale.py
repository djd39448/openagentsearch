"""A bounded byte reader plus the SCALE primitives Appendix F's objects are built from: fixed-width
little-endian integers, `bool`, raw `H256`, and `Compact<u32>` (used here only for `Vec<T>` length
prefixes -- no scalar field in Appendix F is `Compact`-encoded, see `objects.py`).

`Reader` never reads past the end of the buffer it was given and never allocates based on an
attacker-controlled length before bounding it against what a `WireDecodeError` reason exists for
-- every method either returns a value or raises `WireDecodeError`, never silently truncates,
saturates, or wraps. Nothing here is a security boundary in the OS/process sense; it is a decoder
that fails closed on malformed bytes.

NOT guaranteed: this module knows nothing about the *meaning* of the bytes it reads (no domain
separation, no hashing, no object-level consistency rules) -- see `hashes.py` and `objects.py` for
that.
"""

_FIXED_WIDTHS = {"u8": 1, "u16": 2, "u32": 4, "u64": 8, "u128": 16}


class WireDecodeError(ValueError):
    """Raised by every decoder in `openagentsearch.flop.wire` on malformed or inconsistent input.

    `reason` is one of a fixed, exact-string vocabulary (tests match on it directly):
    `"truncated"`, `"overlong"`, `"exceeds u32"`, `"trailing bytes"`, `"invalid bool"`,
    `"unknown tag"`, `"unknown magic"`, `"PathTooLong"`, `"LeafFieldsInconsistent"`,
    `"FccFieldsInconsistent"`, `"UnsupportedLeafVersion"`, `"DuplicateVerifiedTurn"`,
    `"TurnIndexOutOfRange"`, `"LeafNotInRoot"`, `"AggregateGnOverflow"`, `"TooManyTurns"`,
    `"BadReceiptSignature"`, `"BadValidatorSignature"`, `"BadLeafSignature"`.

    `offset` is the byte position in the input at which the decoder detected the problem (not
    always the exact first differing byte for object-level consistency rules -- see each raise
    site's `detail` for specifics). `detail` is free-form, human-readable context; NOT part of the
    matched vocabulary and NOT guaranteed stable across versions of this package.

    `str(exc)` always starts with `reason` -- callers that want to match on reason without
    catching and inspecting `.reason` can do `str(exc).startswith("truncated")` etc., though
    inspecting `.reason` directly is preferred and exact.
    """

    def __init__(self, reason: str, offset: int, detail: str = "") -> None:
        self.reason = reason
        self.offset = offset
        self.detail = detail
        message = f"{reason} at offset {offset}" + (f": {detail}" if detail else "")
        super().__init__(message)


class Reader:
    """A cursor over an immutable `bytes` buffer. Every read either advances the cursor and
    returns bytes/an int/a bool, or raises `WireDecodeError` and leaves the cursor wherever it was
    when the failing read was attempted (a `Reader` is not meant to be reused after an exception).

    NOT guaranteed: no method here validates that the *value* decoded makes semantic sense (a
    `u128` read of all-`0xff` bytes is a perfectly valid read); object-level range/consistency
    checks belong to `objects.py`.
    """

    __slots__ = ("_data", "_pos")

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    @property
    def offset(self) -> int:
        """The cursor's current byte position (0 at construction, `len(data)` once exhausted)."""
        return self._pos

    def remaining(self) -> int:
        """Bytes not yet consumed."""
        return len(self._data) - self._pos

    def take(self, n: int) -> bytes:
        """Consume and return exactly `n` bytes. Raises `WireDecodeError("truncated", ...)` when
        fewer than `n` bytes remain; raises `ValueError` if `n` itself is negative (caller misuse,
        not a wire-decode failure)."""
        if n < 0:
            raise ValueError(f"n must be non-negative, got {n!r}")
        start = self._pos
        end = start + n
        if end > len(self._data):
            raise WireDecodeError(
                "truncated", start, f"need {n} byte(s), {len(self._data) - start} available"
            )
        self._pos = end
        return self._data[start:end]

    def _fixed(self, width_name: str) -> int:
        width = _FIXED_WIDTHS[width_name]
        return int.from_bytes(self.take(width), "little")

    def u8(self) -> int:
        """Fixed 1-byte unsigned little-endian integer (trivially LE)."""
        return self._fixed("u8")

    def u16(self) -> int:
        """Fixed 2-byte unsigned little-endian integer."""
        return self._fixed("u16")

    def u32(self) -> int:
        """Fixed 4-byte unsigned little-endian integer."""
        return self._fixed("u32")

    def u64(self) -> int:
        """Fixed 8-byte unsigned little-endian integer."""
        return self._fixed("u64")

    def u128(self) -> int:
        """Fixed 16-byte unsigned little-endian integer."""
        return self._fixed("u128")

    def bool_(self) -> bool:
        """SCALE `bool`: `0x00` -> `False`, `0x01` -> `True`, anything else ->
        `WireDecodeError("invalid bool", ...)`. All 254 other byte values are rejected uniformly;
        none is special-cased."""
        start = self._pos
        byte = self.take(1)[0]
        if byte == 0x00:
            return False
        if byte == 0x01:
            return True
        raise WireDecodeError("invalid bool", start, f"byte=0x{byte:02x}")

    def h256(self) -> bytes:
        """Raw 32-byte hash, no length prefix, no framing."""
        return self.take(32)

    def compact_u32(self) -> int:
        """`Compact<u32>` (SCALE's LEB128-like compact integer, mode selected by the low 2 bits of
        the first byte), checked in this exact order:

        1. `truncated` -- fewer bytes are available than the selected mode needs.
        2. `exceeds u32` -- mode 3 only: if the declared payload length `n = (byte[0] >> 2) + 4`
           is greater than 4, this is rejected immediately on the declared length alone, *before*
           attempting to read or evaluate the `n` payload bytes -- a `Compact<u32>` target cannot
           hold a value that needs a 5th+ byte no matter what those bytes contain, and checking the
           declared length first (rather than reading `n` bytes and rejecting on the decoded value)
           is what gives `070000000000` (5 all-zero payload bytes) the `"exceeds u32"` reason
           instead of being wrongly caught as `"overlong"`.
        3. `overlong` -- the decoded value would fit in a smaller mode (mode 1 value < 64, mode 2
           value < 16384, mode 3 with `n==4` value < 2**30). Mode 0 is always minimal by
           construction and is never overlong.

        `Compact<u32>` here is used only for `Vec<T>` length prefixes (`merkle_path`,
        `Vec<VerifiedTurn>`) -- no scalar Appendix F field is `Compact`-encoded.
        """
        start = self._pos
        first = self.take(1)[0]
        mode = first & 0b11
        if mode == 0:
            return first >> 2
        if mode == 1:
            rest = self.take(1)
            value = (first | (rest[0] << 8)) >> 2
            if value < 64:
                raise WireDecodeError("overlong", start, f"compact mode 1 value {value} < 64")
            return value
        if mode == 2:
            rest = self.take(3)
            raw = int.from_bytes(bytes([first]) + rest, "little")
            value = raw >> 2
            if value < 16384:
                raise WireDecodeError("overlong", start, f"compact mode 2 value {value} < 16384")
            return value
        # mode == 3
        n = (first >> 2) + 4
        if n > 4:
            raise WireDecodeError(
                "exceeds u32", start, f"compact mode 3 declared payload length {n} > 4"
            )
        payload = self.take(n)
        value = int.from_bytes(payload, "little")
        if n == 4 and value < 2**30:
            raise WireDecodeError(
                "overlong", start, f"compact mode 3 (n=4) value {value} < 2**30"
            )
        return value

    def finish(self) -> None:
        """Raise `WireDecodeError("trailing bytes", ...)` if any byte remains unconsumed; a no-op
        otherwise. Every top-level `decode(data)` classmethod in this package calls this exactly
        once, after fully decoding `data`, so that extra bytes past a fully-decoded object are
        never silently accepted."""
        if self.remaining() > 0:
            raise WireDecodeError(
                "trailing bytes", self._pos, f"{self.remaining()} byte(s) left unconsumed"
            )


def decode_compact_u32(data: bytes) -> int:
    """Decode a `Compact<u32>` from `data`, requiring that decoding consumes `data` exactly (no
    bytes left over -- raises `WireDecodeError("trailing bytes", ...)` otherwise). See
    `Reader.compact_u32` for the full rejection-reason ordering."""
    reader = Reader(data)
    value = reader.compact_u32()
    reader.finish()
    return value


def encode_compact_u32(value: int) -> bytes:
    """Canonical (minimal) `Compact<u32>` encoding of `value`. Raises `ValueError` if `value` is
    not an `int` in `[0, 2**32 - 1]` -- this is a caller-misuse check, not a wire-decode failure.

    For `Compact<u32>` specifically, mode 3 (`n = (byte[0] >> 2) + 4`) can only ever have `n == 4`
    minimally (the smallest possible `n` in mode 3 is 4, since `byte[0] >> 2` is non-negative), so
    every value in `[2**30, 2**32 - 1]` encodes as mode 3 with exactly 4 payload bytes; there is no
    shorter mode-3 encoding to prefer.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"value must be an int, got {value!r}")
    if value < 0 or value > 0xFFFFFFFF:
        raise ValueError(f"value out of u32 range [0, 2**32-1], got {value!r}")
    if value < 64:
        return bytes([(value << 2) | 0])
    if value < 16384:
        return ((value << 2) | 1).to_bytes(2, "little")
    if value < 2**30:
        return ((value << 2) | 2).to_bytes(4, "little")
    return bytes([0x03]) + value.to_bytes(4, "little")
