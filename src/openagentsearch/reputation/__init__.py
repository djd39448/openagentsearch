"""DID reputation ledger (package B1): evidence-weighted scores computed purely from the
technocore.chat message log (`openagentsearch.sources.technocore_messages`).

`facts.py` turns log rows into per-DID `DidFacts` (age, distinct-text ratio, mention edges, burst
detection); `score.py` turns those facts into a deterministic `Score` that a 2,000-identity burst
cannot buy (identity count and post count are never multipliers); `notes.py` reads the optional,
labeled `did-*` note convention (never trusted, never fetched here); `ledger.py` assembles the
typed `Ledger` and its on-disk JSONL file. See `docs/reputation.md` for the exact formula and
`GET /did/{did}` in `docs/api.md` for the shape a future package (B2) will serve from this file --
this package only builds the ledger; nothing here serves it over HTTP.

Standard library only. No network.
"""
