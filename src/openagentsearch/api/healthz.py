"""Store-aware `/healthz` route: reports index-manifest counts by status alongside the plain
`{"status": "ok"}` the default healthz already returns.

Not registered by default. `server.py`'s built-in `/healthz` route stays byte-identical to
`{"status":"ok"}` for every existing caller; a caller opts into this richer route explicitly by
passing `routes={"/healthz": make_healthz_route(store), ...}` to `create_server()`. This route is
deliberately not added to `AGENT_API_CONTRACT` in `openagentsearch.api.contract` — it is an
operational/liveness endpoint, not part of the agent-facing search/doc contract.
"""

from openagentsearch.api.server import JSONRoute
from openagentsearch.vector.store import VectorStore


def make_healthz_route(store: VectorStore) -> JSONRoute:
    """Build a `/healthz` route reporting `{"status": "ok", "index": <manifest counts by status>,
    "kinds": <manifest counts by status, per source_kind>}`.

    Key order is `status`, `index`, then `kinds`; within `index`, key order follows `STATUSES`
    (`indexed`, `failed`, `superseded`, `refused`); `kinds` maps each `source_kind` present in the
    manifest to that same per-status shape, in `source_kind` sort order. A `source_kind` with no
    rows at all does not appear in `kinds` (see `read_manifest_kind_counts`). This does not
    swallow `ManifestCorruptionError` — a broken manifest table surfaces as a request failure
    rather than a false "ok".
    """

    def route(query: dict[str, list[str]]) -> tuple[int, dict[str, object]]:
        kinds = {kc.source_kind: kc.counts.as_dict() for kc in store.manifest_kind_counts()}
        return 200, {"status": "ok", "index": store.manifest_counts().as_dict(), "kinds": kinds}

    return route
