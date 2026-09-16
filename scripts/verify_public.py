"""Verifies a deployed OpenAgentSearch Worker against a local `manifest.json` snapshot: GETs
`BASE_URL/healthz` (bounded body, no redirects followed), compares its `index.indexed` and
per-`kind` indexed counts against the local manifest's `counts.indexed` / `kinds.*.indexed`, then
POSTs one MCP `initialize` and one `tools/list` request to `BASE_URL/mcp` and checks the response
names exactly the three documented tools (`search`, `did_lookup`, `index_info`).

`--ledger PATH` (optional, package B2) additionally checks the deployed reputation ledger against
a local compact artifact (`openagentsearch.reputation.compact`'s `did-ledger-compact.json`): the
live `/healthz`'s `ledger.dids` and `ledger.generated_at` must match the local file's own `dids`/
`generated_at`, and `GET BASE_URL/did/<OUR_DID>` must answer `200` with the same `facts.
first_seen_seq` the local file records for that DID -- the B2 done-when in BUILDSPEC §3.

Usage: python scripts/verify_public.py BASE_URL --manifest PATH [--ledger PATH] [--timeout 20]

Prints exactly one compact JSON line to stdout. Exit `0` on a full match, `1` on the first mismatch
found (the printed line names it under `"reason"`), `2` on a bad argument (before any network
access -- `argparse`'s own behavior). Pure stdlib (`urllib.request`); this is the only piece of
`docs/api.md`'s operator procedure that ever touches a real, deployed Worker -- the deploy step
itself (`npx wrangler deploy`) is never invoked by this script or by any test.

NOT guaranteed: this does not check every field `/healthz` or `/mcp` returns, only the ones a
stale or misconfigured deploy is most likely to get wrong (document counts and the tool list); it
does not retry a transient network failure.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any
from urllib.request import OpenerDirector

MAX_BODY_BYTES = 1_000_000
# Cloudflare's edge answers 403 "error code: 1010" (Browser Integrity Check) to urllib's default
# `Python-urllib/x.y` User-Agent; any explicit value passes. Every request here sends this one.
USER_AGENT = "OpenAgentSearch-verify/1.0"
DEFAULT_TIMEOUT_S = 20.0
EXPECTED_TOOLS = ("did_lookup", "index_info", "search")
# The project's own DID -- present (non-burst) on the live 2026-09-16 log; the same identity
# docs/agent-api.md's did_lookup example and worker/test/router.test.mjs already reference.
OUR_DID = "did:key:z6MkfVWRHNeiV99ckgHDmi8HpwMLtir1XsTu9rNCoYdTuizf"


class VerifyError(Exception):
    """One problem found while verifying `BASE_URL`. `reason` is the one-line, machine-readable
    summary printed to stdout (never the Python exception message shape)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Turns every redirect into the underlying `HTTPError` instead of following it -- a `/healthz`
    or `/mcp` that redirects is treated as a verification failure, never as a hop to chase."""

    def redirect_request(
        self, req: object, fp: object, code: int, msg: str, headers: object, newurl: str
    ) -> None:
        return None


def _opener() -> OpenerDirector:
    return urllib.request.build_opener(_NoRedirectHandler)


def _http_get_json(url: str, timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": USER_AGENT})
    try:
        with _opener().open(request, timeout=timeout) as response:
            body = response.read(MAX_BODY_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            raise VerifyError(
                f"GET {url} redirected (HTTP {exc.code}); redirects are not followed"
            ) from exc
        raise VerifyError(f"GET {url} failed: HTTP {exc.code}") from exc
    except OSError as exc:
        raise VerifyError(f"GET {url} failed: {exc}") from exc
    return _parse_json_object(body, url)


def _http_post_json(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    body_bytes = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body_bytes,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with _opener().open(request, timeout=timeout) as response:
            content_type = response.headers.get("Content-Type", "")
            raw = response.read(MAX_BODY_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            raise VerifyError(
                f"POST {url} redirected (HTTP {exc.code}); redirects are not followed"
            ) from exc
        content_type = exc.headers.get("Content-Type", "") if exc.headers is not None else ""
        raw = exc.read(MAX_BODY_BYTES + 1)
    except OSError as exc:
        raise VerifyError(f"POST {url} failed: {exc}") from exc
    return _parse_rpc_message(raw, content_type, url)


def _parse_json_object(body: bytes, url: str) -> dict[str, Any]:
    if len(body) > MAX_BODY_BYTES:
        raise VerifyError(f"response from {url} exceeds {MAX_BODY_BYTES} bytes")
    try:
        parsed: Any = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerifyError(f"response from {url} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise VerifyError(f"response from {url} is not a JSON object")
    return parsed


def _parse_rpc_message(raw: bytes, content_type: str, url: str) -> dict[str, Any]:
    """`raw` -> the one JSON-RPC message it carries, whether the response is plain
    `application/json` or a one-shot `text/event-stream` (`event: message\\ndata: <json>\\n\\n`) --
    the two shapes `worker/src/index.js`'s MCP transport can answer with for a single-request
    exchange with no server-to-client notifications."""
    if len(raw) > MAX_BODY_BYTES:
        raise VerifyError(f"response from {url} exceeds {MAX_BODY_BYTES} bytes")
    if "text/event-stream" in content_type:
        text = raw.decode("utf-8", errors="replace")
        for block in text.split("\n\n"):
            for line in block.splitlines():
                if line.startswith("data: "):
                    try:
                        parsed: Any = json.loads(line[len("data: ") :])
                    except json.JSONDecodeError as exc:
                        raise VerifyError(f"malformed SSE data frame from {url}: {exc}") from exc
                    if isinstance(parsed, dict):
                        return parsed
                    raise VerifyError(f"SSE data frame from {url} is not a JSON object")
        raise VerifyError(f"no SSE data frame found in response from {url}")
    return _parse_json_object(raw, url)


def _load_manifest(manifest_path: str) -> dict[str, Any]:
    try:
        with open(manifest_path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        raise VerifyError(f"cannot read manifest {manifest_path}: {exc}") from exc
    try:
        parsed: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerifyError(f"manifest {manifest_path} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise VerifyError(f"manifest {manifest_path} is not a JSON object")
    return parsed


def _manifest_indexed_counts(
    manifest: dict[str, Any], manifest_path: str
) -> tuple[int, dict[str, int]]:
    counts = manifest.get("counts")
    if not isinstance(counts, dict) or not isinstance(counts.get("indexed"), int):
        raise VerifyError(f"manifest {manifest_path} has no integer counts.indexed")
    total_indexed = counts["indexed"]

    kinds_raw = manifest.get("kinds")
    if not isinstance(kinds_raw, dict):
        raise VerifyError(f"manifest {manifest_path} has no kinds")
    kinds: dict[str, int] = {}
    for kind, kind_counts in kinds_raw.items():
        if not isinstance(kind_counts, dict) or not isinstance(kind_counts.get("indexed"), int):
            raise VerifyError(
                f"manifest {manifest_path} kinds[{kind!r}] has no integer indexed count"
            )
        kinds[kind] = kind_counts["indexed"]
    return total_indexed, kinds


def _load_local_ledger(ledger_path: str) -> dict[str, Any]:
    """Loads `ledger_path` (a local `did-ledger-compact.json`) as a plain JSON object -- this
    script deliberately never imports `openagentsearch.reputation.compact`, so it keeps working
    with no `PYTHONPATH` set, exactly like every other check here; it only reads the handful of
    fields it needs (`dids`, `generated_at`, and one DID's `facts.first_seen_seq`), not the full
    typed/validated shape `compact.load_compact_ledger` enforces."""
    try:
        with open(ledger_path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        raise VerifyError(f"cannot read ledger {ledger_path}: {exc}") from exc
    try:
        parsed: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerifyError(f"ledger {ledger_path} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise VerifyError(f"ledger {ledger_path} is not a JSON object")
    return parsed


def _local_ledger_first_seen_seq(ledger: dict[str, Any], did: str, ledger_path: str) -> int:
    non_burst = ledger.get("non_burst")
    if isinstance(non_burst, dict):
        row = non_burst.get(did)
        if isinstance(row, dict):
            facts = row.get("facts")
            if isinstance(facts, dict) and isinstance(facts.get("first_seen_seq"), int):
                return facts["first_seen_seq"]
    raise VerifyError(f"ledger {ledger_path} has no non_burst entry with first_seen_seq for {did}")


def verify(
    base_url: str,
    manifest_path: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
    ledger_path: str | None = None,
) -> dict[str, Any]:
    """Runs the full check against `base_url`, raising `VerifyError` naming the first mismatch
    found. Returns a report `dict` (the one printed as JSON) on success. `ledger_path`, when
    given, additionally runs the package B2 ledger checks described in the module docstring."""
    base_url = base_url.rstrip("/")

    manifest = _load_manifest(manifest_path)
    manifest_indexed, manifest_kinds = _manifest_indexed_counts(manifest, manifest_path)

    healthz = _http_get_json(f"{base_url}/healthz", timeout)
    live_index = healthz.get("index")
    if not isinstance(live_index, dict) or not isinstance(live_index.get("indexed"), int):
        raise VerifyError("/healthz response has no integer index.indexed")
    live_indexed = live_index["indexed"]
    live_kinds = healthz.get("kinds")
    if not isinstance(live_kinds, dict):
        raise VerifyError("/healthz response has no kinds")

    if live_indexed != manifest_indexed:
        raise VerifyError(
            f"index.indexed mismatch: live={live_indexed!r} manifest={manifest_indexed!r}"
        )
    if live_kinds != manifest_kinds:
        raise VerifyError(f"kinds mismatch: live={live_kinds!r} manifest={manifest_kinds!r}")

    init_message = _http_post_json(
        f"{base_url}/mcp",
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "verify_public", "version": "1.0.0"},
            },
        },
        timeout,
    )
    if "error" in init_message:
        raise VerifyError(f"MCP initialize failed: {init_message['error']!r}")

    list_message = _http_post_json(
        f"{base_url}/mcp",
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        timeout,
    )
    if "error" in list_message:
        raise VerifyError(f"MCP tools/list failed: {list_message['error']!r}")
    result = list_message.get("result")
    tools = result.get("tools") if isinstance(result, dict) else None
    if not isinstance(tools, list):
        raise VerifyError("MCP tools/list response has no result.tools")
    tool_names: list[str] = []
    for tool in tools:
        if isinstance(tool, dict):
            name = tool.get("name")
            if isinstance(name, str):
                tool_names.append(name)
    tool_names.sort()
    if tuple(tool_names) != tuple(sorted(EXPECTED_TOOLS)):
        raise VerifyError(
            f"tool list mismatch: live={tool_names!r} expected={sorted(EXPECTED_TOOLS)!r}"
        )

    report: dict[str, Any] = {
        "ok": True,
        "base_url": base_url,
        "indexed": live_indexed,
        "kinds": live_kinds,
        "tools": tool_names,
        "generated_at": healthz.get("generated_at"),
        "db_sha256": healthz.get("db_sha256"),
    }

    if ledger_path is not None:
        local_ledger = _load_local_ledger(ledger_path)
        local_dids = local_ledger.get("dids")
        local_generated_at = local_ledger.get("generated_at")
        if not isinstance(local_dids, int):
            raise VerifyError(f"ledger {ledger_path} has no integer dids")
        if not isinstance(local_generated_at, str):
            raise VerifyError(f"ledger {ledger_path} has no string generated_at")

        live_ledger = healthz.get("ledger")
        if not isinstance(live_ledger, dict):
            raise VerifyError("/healthz response has no ledger object")
        if live_ledger.get("dids") != local_dids:
            raise VerifyError(
                f"ledger dids mismatch: live={live_ledger.get('dids')!r} local={local_dids!r}"
            )
        if live_ledger.get("generated_at") != local_generated_at:
            raise VerifyError(
                f"ledger generated_at mismatch: "
                f"live={live_ledger.get('generated_at')!r} local={local_generated_at!r}"
            )

        local_first_seen_seq = _local_ledger_first_seen_seq(local_ledger, OUR_DID, ledger_path)
        # NOT percent-encoded: the Worker's `/did/{did}` route (`worker/src/routes.js`) reads
        # `did` straight off `new URL(request.url).pathname` with no decoding step, so an encoded
        # colon (`%3A`) would fail its `DID_RE` check and this would 400 `invalid_did` against a
        # perfectly correct deploy -- OUR_DID's `did:key:` colons are legal, unescaped characters
        # in a URL path segment and need no escaping here. (The A2 server DOES percent-decode,
        # `openagentsearch.api.did`, but this script's documented target is the deployed Worker.)
        did_url = f"{base_url}/did/{OUR_DID}"
        did_body = _http_get_json(did_url, timeout)
        did_facts = did_body.get("facts")
        if not isinstance(did_facts, dict) or not isinstance(did_facts.get("first_seen_seq"), int):
            raise VerifyError(f"GET {did_url} response has no integer facts.first_seen_seq")
        live_first_seen_seq = did_facts["first_seen_seq"]
        if live_first_seen_seq != local_first_seen_seq:
            raise VerifyError(
                f"first_seen_seq mismatch: "
                f"live={live_first_seen_seq!r} local={local_first_seen_seq!r}"
            )

        report["ledger_dids"] = local_dids
        report["ledger_generated_at"] = local_generated_at
        report["first_seen_seq"] = local_first_seen_seq

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url", help="e.g. https://openagentsearch.<subdomain>.workers.dev")
    parser.add_argument("--manifest", required=True, help="path to a local manifest.json")
    parser.add_argument(
        "--ledger", default=None, help="optional: path to a local did-ledger-compact.json"
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    args = parser.parse_args(argv)  # argparse itself exits 2 on a bad argument, 0 on --help

    try:
        report = verify(
            args.base_url, args.manifest, timeout=args.timeout, ledger_path=args.ledger
        )
    except VerifyError as exc:
        print(json.dumps({"ok": False, "reason": exc.reason}))
        return 1

    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
