"""Store-aware `/healthz` route: reports index-manifest counts by status alongside the plain
`{"status": "ok"}` the default healthz already returns, and (package B2) the reputation ledger's
own counts.

Not registered by default. `server.py`'s built-in `/healthz` route stays byte-identical to
`{"status":"ok"}` for every existing caller; a caller opts into this richer route explicitly by
passing `routes={"/healthz": make_healthz_route(store), ...}` to `create_server()`. This route is
deliberately not added to `AGENT_API_CONTRACT` in `openagentsearch.api.contract` — it is an
operational/liveness endpoint, not part of the agent-facing search/doc contract.
"""

from openagentsearch.api.server import JSONRoute
from openagentsearch.reputation.compact import CompactLedger
from openagentsearch.vector.store import VectorStore


def _ledger_summary(ledger: CompactLedger | None) -> dict[str, object] | None:
    if ledger is None:
        return None
    return {"dids": ledger.dids, "bursts": ledger.bursts, "generated_at": ledger.generated_at}


def make_healthz_route(store: VectorStore, ledger: CompactLedger | None = None) -> JSONRoute:
    """Build a `/healthz` route reporting `{"status": "ok", "index": <manifest counts by status>,
    "kinds": <manifest counts by status, per source_kind>, "ledger": <dids/bursts/generated_at,
    or null>}`.

    Key order is `status`, `index`, `kinds`, then `ledger`; within `index`, key order follows
    `STATUSES` (`indexed`, `failed`, `superseded`, `refused`); `kinds` maps each `source_kind`
    present in the manifest to that same per-status shape, in `source_kind` sort order. A
    `source_kind` with no rows at all does not appear in `kinds` (see
    `read_manifest_kind_counts`). `ledger` is `null` whenever the caller passes no `ledger` (the
    server was started without `--ledger`) — never omitted, so a caller can always tell "no
    ledger loaded" from "an older route that never reported one" by its presence alone. This does
    not swallow `ManifestCorruptionError` — a broken manifest table surfaces as a request failure
    rather than a false "ok".
    """

    def route(query: dict[str, list[str]]) -> tuple[int, dict[str, object]]:
        kinds = {kc.source_kind: kc.counts.as_dict() for kc in store.manifest_kind_counts()}
        return 200, {
            "status": "ok",
            "index": store.manifest_counts().as_dict(),
            "kinds": kinds,
            "ledger": _ledger_summary(ledger),
        }

    return route
