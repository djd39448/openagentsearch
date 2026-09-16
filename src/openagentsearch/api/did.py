"""`/did/{did}` prefix route (package B2): the reputation-ledger lookup, the same JSON shape the
Cloudflare Worker and both MCP tools answer -- see `docs/api.md`'s `GET /did/{did}` contract and
`openagentsearch.reputation.compact` for the loaded artifact this route reads.

NOT guaranteed: this route performs no signature verification of any kind (nothing upstream of it
does either -- see `openagentsearch.reputation.facts.Post`'s own docstring); a score is evidence
from one message log, never an endorsement.
"""

import re
import urllib.parse
from collections.abc import Callable

from openagentsearch.api.server import RouteResult
from openagentsearch.reputation.compact import CompactLedger

# The did:key method, multibase "z" (base58btc) prefix, 1-120 base58 characters after it -- the
# same pattern docs/api.md documents and worker/src/routes.js's DID_RE matches.
DID_RE = re.compile(r"^did:key:z[1-9A-HJ-NP-Za-km-z]{1,120}$")


def make_did_prefix_route(
    ledger: CompactLedger | None,
) -> Callable[[str, dict[str, list[str]]], RouteResult]:
    """Build the `/did/` prefix route (registered under `prefix_routes={"/did/": ..., ...}`, the
    same mechanism `openagentsearch.api.doc.make_doc_route` uses for `/doc/`).

    `remainder` (the path segment after the `/did/` prefix) is percent-decoded before validation,
    so an agent that percent-encodes the `did:key:` colons still reaches the same answer a literal
    one would. `ledger` is the parsed compact artifact (`compact.load_compact_ledger`), or `None`
    when the server was started without `--ledger` -- every well-formed DID then answers `404
    ledger_not_built`, exactly like the Worker's `makeWorker(index, null)`.

    Every response, whatever its status, carries `X-Ledger-Generated-At` whenever `ledger is not
    None` (there is nothing to report it as when no ledger is loaded at all).
    """

    def route(remainder: str, query_dict: dict[str, list[str]]) -> RouteResult:
        extra_headers = {} if ledger is None else {"X-Ledger-Generated-At": ledger.generated_at}
        did = urllib.parse.unquote(remainder)
        if not DID_RE.fullmatch(did):
            return 400, {"error": "invalid_did"}, extra_headers
        if ledger is None:
            return 404, {"error": "ledger_not_built"}, extra_headers
        answer = ledger.lookup(did)
        if answer is None:
            return 404, {"error": "unknown_did"}, extra_headers
        return 200, answer, extra_headers

    return route
