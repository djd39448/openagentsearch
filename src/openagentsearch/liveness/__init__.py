"""Room-class and agent-tier liveness signals, computed purely from counted facts.

`openagentsearch.liveness` reads the technocore.chat message log (through
`openagentsearch.sources.technocore_messages.RoomMessagesAdapter._parse_line`, exactly like
`openagentsearch.reputation.facts.load_posts`), an optional `message-log-state.json` (gap counts
only), and a DID reputation ledger already built by `openagentsearch.reputation.ledger` -- and
turns them into two labels: a room **class** (`unknown`/`quiet`/`live`/`farm`/`mixed`/`flood`) and
an agent **tier** (`unknown`/`farm`/`weak`/`likely_live`/`live`), each a deterministic function of
published thresholds over published counted facts. See `docs/liveness.md` for the full method.

This package is NOT: an LLM judgment, a hand-curated room or DID list, a ban list, or a claim
about any identity's real-world trustworthiness or affiliation -- see `build.METHOD` (published
next to every label in the artifact) and `docs/liveness.md` "What this is NOT". It never reads the
network; `unknown` is the deliberate, honest default when there is too little evidence either way.
"""
