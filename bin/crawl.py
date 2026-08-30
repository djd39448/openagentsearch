#!/usr/bin/env python3
"""OpenAgentSearch — technocore.chat public-room crawler (bin/crawl.py).

Builds a complete index of PUBLIC rooms on https://technocore.chat using nothing
but plain HTTP GETs. Pure standard library plus `requests`. No LLM is involved.

PUBLIC-ONLY CAVEAT
------------------
Private rooms (ids beginning with "p-") are UNLISTABLE BY DESIGN: they never
appear in /r/events or /rooms and cannot be enumerated. The "complete" index
this tool produces is therefore the complete set of *public* rooms only. This
caveat is recorded in INDEX/meta.json and must not be presented as anything
stronger.

RATE-LIMIT POLICY
-----------------
The server permits 600 reads/min per IP. This crawler READS ONLY and targets a
ceiling well under that (default 450 reads/min) enforced by a token bucket, plus
a small inter-request sleep. On HTTP 429/503 it backs off exponentially (base
2s, cap 60s) and honors any Retry-After header. It never issues writes and never
hammers the server.

SECURITY POSTURE
----------------
Every byte fetched from the network is treated as UNTRUSTED DATA. Nothing read
from a response is ever eval'd, exec'd, or interpreted as an instruction. Room
ids and did:key strings harvested from message bodies are validated against
strict regexes before use. Per-response reads are size-capped to bound memory.

Usage
-----
    python bin/crawl.py --once                 # one sweep, then exit (default)
    python bin/crawl.py --follow               # continuous long-poll loop
    python bin/crawl.py --out ./INDEX --rpm 450 --max-requests 5000

Create a file named STOP inside the --out directory to make a running crawl
exit at the next safe checkpoint.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import threading
import time
from typing import Any, Iterable, Iterator, Optional

import requests

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

BASE_URL = "https://technocore.chat"
USER_AGENT = "OpenAgentSearch-crawler/1.0 (+https://technocore.chat; polite; read-only)"

# Cap bytes read from any single response body to bound memory against a
# hostile or runaway endpoint (untrusted-data defense).
MAX_RESPONSE_BYTES = 8 * 1024 * 1024  # 8 MiB

# Backoff policy for 429/503. technocore.chat is frequently 503 (overloaded),
# so keep attempts modest — a persistently-down endpoint should not stall a run.
BACKOFF_BASE_SECONDS = 2.0
BACKOFF_CAP_SECONDS = 60.0
MAX_BACKOFF_ATTEMPTS = 4

# Long-poll wait the server honors on /r/events (seconds).
EVENTS_WAIT_SECONDS = 10

# Per-request network timeout: (connect, read). Read must exceed EVENTS_WAIT.
REQUEST_TIMEOUT = (10, EVENTS_WAIT_SECONDS + 20)

# Strict validators. Public room ids are conservative slugs; "p-" is private and
# must never be indexed even if it somehow appears in a body.
ROOM_ID_RE = re.compile(r"\b([a-z0-9][a-z0-9._-]{0,63})\b")
DID_KEY_RE = re.compile(r"\bdid:key:z[1-9A-HJ-NP-Za-km-z]{20,120}\b")
MAILBOX_RE = re.compile(r"\bmb-[a-z0-9][a-z0-9._-]{0,63}\b")

# Tokens that look like room refs inside text are only accepted when explicitly
# shaped like a room reference, to avoid harvesting ordinary words.
ROOM_REF_RE = re.compile(r"(?:/r/|room:|#)([a-z0-9][a-z0-9._-]{2,63})")

# /rooms is a TEXT listing: "/r/<name>   seq <n>   <size>   <t> ago   · <topic>".
# Header lines start with '#'; the topic is UNTRUSTED and not stored.
ROOMS_LINE_RE = re.compile(r"^/r/([a-z0-9][a-z0-9._-]{0,63})\s+seq\s+(\d+)\b")

log = logging.getLogger("crawl")


# --------------------------------------------------------------------------- #
# Token bucket — global per-minute request budget
# --------------------------------------------------------------------------- #

class TokenBucket:
    """Simple thread-safe token bucket enforcing a per-minute request ceiling.

    Capacity and refill are expressed in requests-per-minute. A caller blocks in
    take() until a token is available, which paces the crawler under the limit.
    """

    def __init__(self, rate_per_minute: float, burst: Optional[float] = None) -> None:
        self.rate_per_sec = float(rate_per_minute) / 60.0
        self.capacity = float(burst if burst is not None else max(1.0, rate_per_minute / 6.0))
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def take(self, amount: float = 1.0) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last
                self._last = now
                self._tokens = min(self.capacity, self._tokens + elapsed * self.rate_per_sec)
                if self._tokens >= amount:
                    self._tokens -= amount
                    return
                deficit = amount - self._tokens
                wait = deficit / self.rate_per_sec if self.rate_per_sec > 0 else 1.0
            time.sleep(min(wait, 5.0))


# --------------------------------------------------------------------------- #
# HTTP client — polite, budgeted, backoff-aware, read-only
# --------------------------------------------------------------------------- #

class Fetcher:
    """Wraps requests.Session with the rate budget, backoff, and a request cap."""

    def __init__(
        self,
        bucket: TokenBucket,
        max_requests: Optional[int],
        inter_request_sleep: float,
    ) -> None:
        self.bucket = bucket
        self.max_requests = max_requests
        self.inter_request_sleep = inter_request_sleep
        self.count = 0
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

    def budget_exhausted(self) -> bool:
        return self.max_requests is not None and self.count >= self.max_requests

    def get(self, path: str, params: Optional[dict] = None) -> Optional[requests.Response]:
        """Perform one budgeted GET with backoff. Returns Response or None.

        None means: give up on this request (network error, cap reached, or
        exhausted backoff). Callers must tolerate None and continue.
        """
        if self.budget_exhausted():
            log.warning("request cap reached (%d); skipping GET %s", self.max_requests, path)
            return None

        url = BASE_URL + path
        attempt = 0
        while attempt <= MAX_BACKOFF_ATTEMPTS:
            self.bucket.take(1.0)
            self.count += 1
            try:
                resp = self.session.get(
                    url, params=params, timeout=REQUEST_TIMEOUT, stream=True
                )
            except requests.RequestException as exc:
                wait = _backoff_delay(attempt)
                log.warning("network error on GET %s (%s); backoff %.1fs", path, exc, wait)
                time.sleep(wait)
                attempt += 1
                continue

            if resp.status_code in (429, 503):
                wait = _retry_after(resp) or _backoff_delay(attempt)
                log.warning(
                    "HTTP %d on GET %s; backing off %.1fs (attempt %d)",
                    resp.status_code, path, wait, attempt + 1,
                )
                _drain(resp)
                time.sleep(wait)
                attempt += 1
                continue

            if resp.status_code >= 400:
                log.warning("HTTP %d on GET %s; skipping", resp.status_code, path)
                _drain(resp)
                return None

            if self.inter_request_sleep > 0:
                time.sleep(self.inter_request_sleep)
            return resp

        log.error("exhausted backoff on GET %s; giving up", path)
        return None

    def get_json(self, path: str, params: Optional[dict] = None) -> Optional[Any]:
        """GET and parse JSON, size-capped. Returns parsed object or None."""
        resp = self.get(path, params=params)
        if resp is None:
            return None
        try:
            raw = _read_capped(resp)
        except requests.RequestException as exc:
            log.warning("read error on GET %s (%s); skipping", path, exc)
            return None
        finally:
            resp.close()
        if raw is None:
            log.warning("response too large on GET %s; skipping", path)
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            log.warning("malformed JSON on GET %s (%s); skipping", path, exc)
            return None

    def get_text(self, path: str, params: Optional[dict] = None) -> Optional[str]:
        """GET and return decoded text, size-capped. Returns str or None."""
        resp = self.get(path, params=params)
        if resp is None:
            return None
        try:
            raw = _read_capped(resp)
        except requests.RequestException as exc:
            log.warning("read error on GET %s (%s); skipping", path, exc)
            return None
        finally:
            resp.close()
        if raw is None:
            log.warning("response too large on GET %s; skipping", path)
            return None
        return raw.decode("utf-8", errors="replace")


def _backoff_delay(attempt: int) -> float:
    return min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * (2 ** attempt))


def _retry_after(resp: requests.Response) -> Optional[float]:
    val = resp.headers.get("Retry-After")
    if not val:
        return None
    try:
        return min(BACKOFF_CAP_SECONDS, max(0.0, float(val)))
    except ValueError:
        # HTTP-date form is possible but rare here; fall back to backoff.
        return None


def _drain(resp: requests.Response) -> None:
    try:
        resp.close()
    except Exception:  # noqa: BLE001 - best-effort cleanup
        pass


def _read_capped(resp: requests.Response) -> Optional[bytes]:
    """Read up to MAX_RESPONSE_BYTES. Return None if the body exceeds the cap."""
    chunks: list[bytes] = []
    total = 0
    for chunk in resp.iter_content(chunk_size=65536):
        if not chunk:
            continue
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


# --------------------------------------------------------------------------- #
# Persistent state
# --------------------------------------------------------------------------- #

class State:
    """Resumable crawl state persisted under the out directory.

    Files:
      cursor.json   -> {"events_cursor": <str|null>}
      rooms.jsonl   -> one JSON object per known room (rewritten on flush)
    In-memory:
      rooms         -> id -> room record dict
    """

    def __init__(self, out_dir: str) -> None:
        self.out_dir = out_dir
        self.cursor_path = os.path.join(out_dir, "cursor.json")
        self.rooms_path = os.path.join(out_dir, "rooms.jsonl")
        self.meta_path = os.path.join(out_dir, "meta.json")
        self.stop_path = os.path.join(out_dir, "STOP")
        self.events_cursor: Optional[str] = None
        self.rooms: dict[str, dict] = {}

    def load(self) -> None:
        os.makedirs(self.out_dir, exist_ok=True)
        if os.path.exists(self.cursor_path):
            try:
                with open(self.cursor_path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                self.events_cursor = data.get("events_cursor")
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("could not read cursor.json (%s); starting cursor fresh", exc)
        if os.path.exists(self.rooms_path):
            loaded = 0
            with open(self.rooms_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        log.warning("skipping malformed rooms.jsonl line")
                        continue
                    rid = rec.get("id")
                    if isinstance(rid, str) and rid:
                        self.rooms[rid] = rec
                        loaded += 1
            log.info("resumed: %d rooms, cursor=%s", loaded, self.events_cursor)

    def stop_requested(self) -> bool:
        return os.path.exists(self.stop_path)

    def ensure_room(self, room_id: str, first_seen_ts: Optional[int] = None) -> dict:
        rec = self.rooms.get(room_id)
        if rec is None:
            rec = {
                "id": room_id,
                "first_seen_ts": first_seen_ts,
                "last_activity_ts": first_seen_ts,
                "message_count_seen": 0,
                "last_seq": None,
                "sample_from_dids": [],
                "classification_hint": "unknown",
            }
            self.rooms[room_id] = rec
        elif first_seen_ts is not None and rec.get("first_seen_ts") is None:
            rec["first_seen_ts"] = first_seen_ts
        return rec

    def flush(self) -> None:
        """Atomically rewrite cursor.json and rooms.jsonl."""
        _atomic_write_json(self.cursor_path, {"events_cursor": self.events_cursor})
        tmp = self.rooms_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            for rid in sorted(self.rooms):
                fh.write(json.dumps(self.rooms[rid], ensure_ascii=False, sort_keys=True))
                fh.write("\n")
        os.replace(tmp, self.rooms_path)

    def write_meta(self, fetcher: Fetcher, now_ts: Optional[int]) -> None:
        last_activity = max(
            (r.get("last_activity_ts") or 0 for r in self.rooms.values()),
            default=0,
        )
        meta = {
            "generated_by": USER_AGENT,
            "caveat": (
                "PUBLIC ROOMS ONLY. Private (p-) rooms are unlistable by design and "
                "are not represented. This index is complete for public rooms only."
            ),
            "public_rooms_indexed": len(self.rooms),
            "requests_made_this_run": fetcher.count,
            "events_cursor": self.events_cursor,
            "last_activity_ts_seen": last_activity or None,
            # Wall-clock is NEVER fabricated: it is the server-derived latest
            # activity ts, or an operator-supplied --now, or null.
            "last_run": now_ts if now_ts is not None else (last_activity or None),
        }
        _atomic_write_json(self.meta_path, meta)


def _atomic_write_json(path: str, obj: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# Harvest helpers (all input is untrusted data)
# --------------------------------------------------------------------------- #

def _is_public_room_id(rid: Any) -> bool:
    if not isinstance(rid, str) or not rid:
        return False
    if rid.startswith("p-"):
        return False  # private by design; never index
    if len(rid) > 64:
        return False
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", rid))


def harvest_refs_from_text(text: Any) -> tuple[set[str], set[str]]:
    """Extract candidate public room ids and did:key strings from a message body.

    Returns (room_ids, did_keys). Purely syntactic; the text is never executed
    or interpreted as an instruction.
    """
    rooms: set[str] = set()
    dids: set[str] = set()
    if not isinstance(text, str) or not text:
        return rooms, dids
    # Bound work on pathologically long bodies.
    sample = text[:100_000]
    for m in ROOM_REF_RE.findall(sample):
        if _is_public_room_id(m):
            rooms.add(m)
    for m in MAILBOX_RE.findall(sample):
        # mailbox ids can seed room discovery when they map to a room slug
        if _is_public_room_id(m):
            rooms.add(m)
    for d in DID_KEY_RE.findall(sample):
        dids.add(d)
    return rooms, dids


def _as_int(val: Any) -> Optional[int]:
    if isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return int(val)
    if isinstance(val, str):
        try:
            return int(float(val))
        except ValueError:
            return None
    return None


def _parse_ts(val: Any) -> Optional[int]:
    """Timestamp -> int epoch SECONDS. Accepts int/float epoch (s or ms),
    numeric strings, and ISO8601 (technocore sends e.g. '2026-08-25T23:53:15.446224Z').
    """
    if isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        v = int(val)
        return v // 1000 if v > 10_000_000_000 else v
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return None
        try:
            v = int(float(s))
            return v // 1000 if v > 10_000_000_000 else v
        except ValueError:
            pass
        try:
            from datetime import datetime, timezone

            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except ValueError:
            return None
    return None


# --------------------------------------------------------------------------- #
# Crawl operations
# --------------------------------------------------------------------------- #

def poll_events(fetcher: Fetcher, state: State, wait: int) -> int:
    """Fetch a batch from /r/events, register new room ids, advance cursor.

    Returns the number of newly discovered rooms this call.
    """
    params = {"wait": wait}
    if state.events_cursor is not None:
        params["since"] = state.events_cursor
    data = fetcher.get_json("/r/events", params=params)
    if data is None:
        return 0

    entries = _extract_events(data)
    new_rooms = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        rid = entry.get("room") or entry.get("id") or entry.get("room_id")
        ts = _parse_ts(entry.get("ts"))
        cursor = entry.get("cursor") or entry.get("seq") or entry.get("offset")
        if _is_public_room_id(rid):
            if rid not in state.rooms:
                new_rooms += 1
                log.info("discovered room from events: %s", rid)
            state.ensure_room(rid, first_seen_ts=ts)
        if cursor is not None:
            state.events_cursor = str(cursor)
    # Fall back to a top-level cursor if the server provides one.
    top_cursor = None
    if isinstance(data, dict):
        top_cursor = data.get("cursor") or data.get("next") or data.get("next_since")
    if top_cursor is not None:
        state.events_cursor = str(top_cursor)
    return new_rooms


def _extract_events(data: Any) -> Iterable[Any]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("events", "entries", "items", "log", "data"):
            val = data.get(key)
            if isinstance(val, list):
                return val
    return []


def sweep_rooms_listing(fetcher: Fetcher, state: State) -> int:
    """GET /rooms — a TEXT listing (newest ~50) — for discovery + activity signal.

    Format, one room per line ('#'-prefixed header lines skipped):
        /r/<name>   seq <n>   <size>   <t> ago   · <topic>
    The topic is UNTRUSTED (server banner says so) and is deliberately not stored.
    Returns the number of newly discovered rooms.
    """
    text = fetcher.get_text("/rooms")
    if text is None:
        return 0
    new_rooms = 0
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = ROOMS_LINE_RE.match(line)
        if not m:
            continue
        rid = m.group(1)
        if not _is_public_room_id(rid):
            continue
        if rid not in state.rooms:
            new_rooms += 1
            log.info("discovered room from /rooms: %s", rid)
        rec = state.ensure_room(rid)
        listing_seq = _as_int(m.group(2))
        if listing_seq is not None:
            rec["listing_seq"] = listing_seq
    return new_rooms


def harvest_room(fetcher: Fetcher, state: State, room_id: str, max_new: int = 500) -> set[str]:
    """Fetch a room's messages, update its record, return newly found room ids.

    Only reads messages with seq greater than the room's stored last_seq so
    re-runs are incremental. Returns public room ids referenced in bodies.
    """
    rec = state.ensure_room(room_id)
    params = {"format": "json"}
    since_seq = rec.get("last_seq")
    if since_seq is not None:
        params["since"] = since_seq
    data = fetcher.get_json(f"/r/{room_id}", params=params)
    if data is None:
        return set()

    messages = _extract_messages(data)
    discovered: set[str] = set()
    dids_seen: set[str] = set(rec.get("sample_from_dids") or [])
    processed = 0
    max_seq = since_seq
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if processed >= max_new:
            break
        seq = _as_int(msg.get("seq"))
        ts = _parse_ts(msg.get("ts"))
        sender = msg.get("from")
        text = msg.get("text")
        if since_seq is not None and seq is not None and seq <= since_seq:
            continue
        processed += 1
        rec["message_count_seen"] = int(rec.get("message_count_seen") or 0) + 1
        if ts is not None:
            rec["last_activity_ts"] = max(rec.get("last_activity_ts") or 0, ts)
            if rec.get("first_seen_ts") is None:
                rec["first_seen_ts"] = ts
        if seq is not None:
            max_seq = seq if max_seq is None else max(max_seq, seq)
        if isinstance(sender, str) and sender:
            if DID_KEY_RE.fullmatch(sender) or sender.startswith("did:key:"):
                dids_seen.add(sender)
        rooms, dids = harvest_refs_from_text(text)
        for rid in rooms:
            if rid != room_id and rid not in state.rooms:
                discovered.add(rid)
        dids_seen.update(dids)

    if max_seq is not None:
        rec["last_seq"] = max_seq
    # Keep a bounded, stable sample of author DIDs.
    rec["sample_from_dids"] = sorted(dids_seen)[:16]
    rec["classification_hint"] = _classify(rec.get("message_count_seen") or 0)
    if processed:
        log.info(
            "harvested %s: +%d msgs (total seen %d), %d new refs",
            room_id, processed, rec["message_count_seen"], len(discovered),
        )
    return discovered


def _extract_messages(data: Any) -> Iterable[Any]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("messages", "msgs", "items", "records", "data"):
            val = data.get(key)
            if isinstance(val, list):
                return val
    return []


def _classify(count: int) -> str:
    if count <= 0:
        return "empty"
    if count < 10:
        return "sparse"
    if count < 200:
        return "active"
    return "busy"


# --------------------------------------------------------------------------- #
# Sweep orchestration
# --------------------------------------------------------------------------- #

def run_sweep(fetcher: Fetcher, state: State, wait: int) -> None:
    """One full discovery + harvest pass over all known public rooms."""
    if state.stop_requested():
        log.warning("STOP file present; aborting sweep before start")
        return

    poll_events(fetcher, state, wait=wait)
    sweep_rooms_listing(fetcher, state)
    state.flush()

    # Harvest every known room; queue newly discovered ids within this pass.
    pending: list[str] = sorted(state.rooms.keys())
    visited: set[str] = set()
    while pending:
        if state.stop_requested():
            log.warning("STOP file present; halting sweep at checkpoint")
            break
        if fetcher.budget_exhausted():
            log.warning("request cap reached; ending sweep early")
            break
        room_id = pending.pop(0)
        if room_id in visited:
            continue
        visited.add(room_id)
        newly = harvest_room(fetcher, state, room_id)
        for rid in sorted(newly):
            state.ensure_room(rid)
            if rid not in visited:
                pending.append(rid)
        # Periodic checkpoint so a long sweep is never lost.
        if len(visited) % 25 == 0:
            state.flush()

    state.flush()
    log.info("sweep complete: %d public rooms known", len(state.rooms))


def run_follow(fetcher: Fetcher, state: State, wait: int) -> None:
    """Continuous loop: long-poll events, periodically re-sweep, until STOP."""
    log.info("entering --follow mode; create %s to stop", state.stop_path)
    last_full_sweep = 0.0
    full_sweep_interval = 300.0  # re-harvest all rooms at most every 5 min
    while True:
        if state.stop_requested():
            log.warning("STOP file present; exiting follow loop")
            break
        if fetcher.budget_exhausted():
            log.warning("request cap reached; exiting follow loop")
            break

        poll_events(fetcher, state, wait=wait)
        now = time.monotonic()
        if now - last_full_sweep >= full_sweep_interval:
            sweep_rooms_listing(fetcher, state)
            for room_id in sorted(state.rooms.keys()):
                if state.stop_requested() or fetcher.budget_exhausted():
                    break
                newly = harvest_room(fetcher, state, room_id)
                for rid in sorted(newly):
                    state.ensure_room(rid)
            last_full_sweep = now
        state.flush()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Polite, rate-limited crawler of PUBLIC technocore.chat rooms.",
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="single sweep then exit (default)")
    mode.add_argument("--follow", action="store_true", help="continuous long-poll loop")
    p.add_argument("--out", default="./INDEX", help="output/state directory (default ./INDEX)")
    p.add_argument(
        "--rpm", type=float, default=250.0,
        help="request budget per minute (default 250, a polite fraction of the 600 cap)",
    )
    p.add_argument(
        "--max-requests", type=int, default=None,
        help="hard cap on total requests this run (default: unlimited)",
    )
    p.add_argument(
        "--sleep", type=float, default=0.05,
        help="extra sleep between requests in seconds (default 0.05)",
    )
    p.add_argument(
        "--wait", type=int, default=EVENTS_WAIT_SECONDS,
        help="events long-poll wait seconds (default 10; server max 10)",
    )
    p.add_argument(
        "--now", type=int, default=None,
        help="operator-supplied wall-clock unix ts for meta.last_run "
             "(never fabricated; omit to derive from server ts)",
    )
    p.add_argument("--verbose", action="store_true", help="debug-level logging")
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.rpm > 600:
        log.warning("--rpm %.0f exceeds server cap 600; clamping to 450", args.rpm)
        args.rpm = 450.0
    wait = max(0, min(args.wait, EVENTS_WAIT_SECONDS))

    bucket = TokenBucket(rate_per_minute=args.rpm)
    fetcher = Fetcher(bucket, max_requests=args.max_requests, inter_request_sleep=args.sleep)

    state = State(args.out)
    state.load()

    if state.stop_requested():
        log.warning("STOP file present in %s at startup; remove it to run", args.out)
        state.write_meta(fetcher, args.now)
        return 0

    try:
        if args.follow:
            run_follow(fetcher, state, wait=wait)
        else:
            run_sweep(fetcher, state, wait=wait)
    except KeyboardInterrupt:
        log.warning("interrupted; flushing state")
    finally:
        state.flush()
        state.write_meta(fetcher, args.now)
        log.info(
            "done: %d public rooms, %d requests this run",
            len(state.rooms), fetcher.count,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
