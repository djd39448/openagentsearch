"""FLOP v1 wire-format decoders (Appendix F): a pure, fail-closed decode-and-recompute library for
the public FLOP v1 wire objects (`DataRef`, `DecodePolicy`, `VerifiedTurn` V0-V3 leaves, the FCC4
DA transcript container, the agent receipt, `ValidatorAttestation`) so that a router can *read and
check* what the chain and SDKs emit.

What this package is NOT and does NOT guarantee -- see each submodule's docstring for detail:

- No claim of conformance beyond the public `tests/fixtures/flop/wire-format-v1.json` corpus
  (`openagentsearch.flop.wire` implements exactly the byte layouts that corpus exercises; a real
  FLOP deployment may extend or change these in ways this package cannot detect).
- No sr25519 signature verification -- no sr25519 implementation exists in the Python standard
  library. `openagentsearch.flop.wire.verify.NoVerifier` (the default) makes every signature check
  report `"not_verified"`; a real answer requires an injected `SignatureVerifier`.
- No chain state and no network. `channel_has_decode_policy` and similar channel-state facts are
  caller-supplied inputs to `openagentsearch.flop.wire.settle`, never looked up here.
- No claim about the F.4 audit-challenge hash (`blake2(audit_id || shard)`) -- the corpus has no
  vector for it, so it is out of this package's tested scope entirely.

See `docs/flop-wire.md` for the full reason vocabulary and hash table.
"""
