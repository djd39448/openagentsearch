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
    """Build a `/healthz` route reporting `{"status": "ok", "index": <manifest counts by status>}`.

    Key order is `status` then `index`; within `index`, key order follows `STATUSES` (`indexed`,
    `failed`, `superseded`, `refused`). This does not swallow `ManifestCorruptionError` — a broken
    manifest table surfaces as a request failure rather than a false "ok".
    """

    def route(query: dict[str, list[str]]) -> tuple[int, dict[str, object]]:
        return 200, {"status": "ok", "index": store.manifest_counts().as_dict()}

    return route
