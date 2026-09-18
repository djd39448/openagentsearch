"""Package ML done-when: `bin/message_log.py`'s `run_sweep` core and its CLI.

`bin/message_log.py` is a script, not a package module, so it is loaded by path (same pattern as
`tests/test_crawl_refs.py` uses for `bin/crawl.py`). Every scenario except the two loopback-server
ones uses an injected fetcher and opens no socket at all; the loopback-server scenarios run a real
`http.server` on 127.0.0.1:0 in a background thread, always stopped in a `finally` block so a bug
here cannot hang the suite.
"""

import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from openagentsearch.pipeline.ingest import FetchResponse
from openagentsearch.sources.technocore_messages import MessageLog

REPO = Path(__file__).resolve().parents[1]
_MESSAGE_LOG_PY = REPO / "bin" / "message_log.py"
_spec = importlib.util.spec_from_file_location("oas_message_log", _MESSAGE_LOG_PY)
message_log = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
# dataclasses' own field-type resolution looks the defining module up in sys.modules by name
# (see dataclasses._is_type), so this module must be registered there before exec_module runs
# any ``@dataclass`` decorator in bin/message_log.py -- otherwise it raises AttributeError on a
# None lookup.
sys.modules[_spec.name] = message_log
_spec.loader.exec_module(message_log)


def _payload_bytes(room: str, first_seq: int | None, last_seq: int | None, messages: list) -> bytes:
    return json.dumps(
        {
            "room": room, "count": len(messages), "first_seq": first_seq, "last_seq": last_seq,
            "generation": 1, "messages": messages,
        }
    ).encode("utf-8")


def _msg(seq: int, sender: str = "did:key:zAAA", text: str = "hi") -> dict:
    return {"seq": seq, "ts": f"t{seq}", "from": sender, "text": text}


class _FakeClock:
    """Deterministic, monotonically increasing clock -- never wall time."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self._t = start

    def __call__(self) -> float:
        self._t += 1.0
        return self._t


def _no_sleep(_seconds: float) -> None:
    pass


class _SleepRecorder:
    """Records every `sleep` duration requested, in order, without actually sleeping."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class _QueueFetcher:
    """Records every URL requested (in order) and answers from a per-URL FIFO queue. A queued
    entry may be a `FetchResponse` (returned) or an `Exception` instance (raised), so retry
    scenarios can be scripted. Raises if a URL is requested that nothing queued -- catches a
    query-string mistake immediately rather than hanging or returning a confusing empty body."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._queues: dict[str, list[FetchResponse | Exception]] = {}

    def queue(self, url: str, response: FetchResponse | Exception) -> None:
        self._queues.setdefault(url, []).append(response)

    def __call__(
        self, url: str, timeout_s: float, max_bytes: int, user_agent: str
    ) -> FetchResponse:
        self.calls.append(url)
        pending = self._queues.get(url)
        if not pending:
            raise AssertionError(f"unexpected URL requested (nothing queued): {url}")
        item = pending.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


# ============================================================ 5a. run_sweep, injected fetcher


def test_run_sweep_two_rooms_since_honoured_then_gap_recorded_and_still_appends(tmp_path):
    base_url = "https://fake.technocore.test"
    room_a, room_b = "room-a", "room-b"
    log = MessageLog(tmp_path)
    fetcher = _QueueFetcher()
    clock = _FakeClock()

    # ---- sweep 1: no prior state, so no &since= on either URL ----
    url_a1 = f"{base_url}/r/{room_a}?format=json&limit=200"
    url_b1 = f"{base_url}/r/{room_b}?format=json&limit=200"
    fetcher.queue(
        url_a1,
        FetchResponse(200, "application/json", _payload_bytes(room_a, 1, 2, [
            _msg(1), _msg(2),
        ]), False),
    )
    fetcher.queue(
        url_b1,
        FetchResponse(200, "application/json", _payload_bytes(room_b, 1, 1, [_msg(1)]), False),
    )

    report1 = message_log.run_sweep(
        [room_a, room_b], log=log, fetch=fetcher, clock=clock, sleep=_no_sleep,
        interval_s=0.0, limit=200, base_url=base_url,
    )
    assert fetcher.calls == [url_a1, url_b1]
    assert report1.rooms == 2
    assert report1.new == 3
    assert report1.duplicates == 0
    assert report1.gaps == 0
    assert report1.errors == ()
    assert log.last_seq(room_a) == 2
    assert log.last_seq(room_b) == 1

    # ---- sweep 2: &since=<last_seq> is honoured for each room ----
    url_a2 = f"{base_url}/r/{room_a}?format=json&limit=200&since=2"
    url_b2 = f"{base_url}/r/{room_b}?format=json&limit=200&since=1"
    fetcher.queue(
        url_a2,
        FetchResponse(200, "application/json", _payload_bytes(room_a, 3, 3, [_msg(3)]), False),
    )
    fetcher.queue(
        url_b2,
        FetchResponse(200, "application/json", _payload_bytes(room_b, 1, 1, [_msg(1)]), False),
    )

    report2 = message_log.run_sweep(
        [room_a, room_b], log=log, fetch=fetcher, clock=clock, sleep=_no_sleep,
        interval_s=0.0, limit=200, base_url=base_url,
    )
    assert fetcher.calls[-2:] == [url_a2, url_b2]
    assert report2.new == 1  # room-a's seq 3 only; room-b's seq 1 is a duplicate
    assert report2.duplicates == 1
    assert report2.gaps == 0
    assert log.last_seq(room_a) == 3

    # ---- sweep 3: the server's window skips ahead -> a gap is recorded, log still appends ----
    url_a3 = f"{base_url}/r/{room_a}?format=json&limit=200&since=3"
    url_b3 = f"{base_url}/r/{room_b}?format=json&limit=200&since=1"
    fetcher.queue(
        url_a3,
        FetchResponse(
            200, "application/json", _payload_bytes(room_a, 10, 10, [_msg(10)]), False
        ),
    )
    fetcher.queue(
        url_b3,
        FetchResponse(200, "application/json", _payload_bytes(room_b, 1, 1, []), False),
    )

    report3 = message_log.run_sweep(
        [room_a, room_b], log=log, fetch=fetcher, clock=clock, sleep=_no_sleep,
        interval_s=0.0, limit=200, base_url=base_url,
    )
    assert report3.gaps == 1
    assert report3.new == 1
    assert log.last_seq(room_a) == 10
    jsonl_lines = (tmp_path / "messages" / f"{room_a}.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    assert len(jsonl_lines) == 4  # 2 + 1 + 1: the gap did not stop the log from appending


def test_run_sweep_records_non_200_and_transport_errors_per_room_without_aborting(tmp_path):
    base_url = "https://fake.technocore.test"
    log = MessageLog(tmp_path)
    fetcher = _QueueFetcher()
    url_bad = f"{base_url}/r/broken-room?format=json&limit=200"
    url_ok = f"{base_url}/r/ok-room?format=json&limit=200"
    fetcher.queue(url_bad, FetchResponse(503, "text/plain", b"nope", False))
    fetcher.queue(
        url_ok,
        FetchResponse(200, "application/json", _payload_bytes("ok-room", 1, 1, [_msg(1)]), False),
    )

    report = message_log.run_sweep(
        ["broken-room", "ok-room"], log=log, fetch=fetcher, clock=_FakeClock(), sleep=_no_sleep,
        interval_s=0.0, limit=200, base_url=base_url,
    )
    assert report.new == 1
    assert "broken-room" in dict(report.errors)
    assert log.last_seq("ok-room") == 1


def test_run_sweep_never_requests_a_private_room(tmp_path):
    log = MessageLog(tmp_path)
    fetcher = _QueueFetcher()  # nothing queued: any __call__ raises AssertionError

    report = message_log.run_sweep(
        ["p-secret"], log=log, fetch=fetcher, clock=_FakeClock(), sleep=_no_sleep,
        interval_s=0.0, limit=200, base_url="https://fake.technocore.test",
    )
    assert fetcher.calls == []
    assert "p-secret" in dict(report.errors)
    assert report.new == 0


# ================================================= 5a-retry. run_sweep retries and timeout_s


def test_run_sweep_retries_after_one_transport_failure_then_succeeds(tmp_path):
    log = MessageLog(tmp_path)
    fetcher = _QueueFetcher()
    url = "https://fake.technocore.test/r/room-a?format=json&limit=200"
    fetcher.queue(url, OSError("connection reset"))
    fetcher.queue(
        url,
        FetchResponse(200, "application/json", _payload_bytes("room-a", 1, 1, [_msg(1)]), False),
    )
    sleeps = _SleepRecorder()

    report = message_log.run_sweep(
        ["room-a"], log=log, fetch=fetcher, clock=_FakeClock(), sleep=sleeps,
        interval_s=0.0, base_url="https://fake.technocore.test", retries=2, retry_backoff_s=5.0,
    )
    assert fetcher.calls == [url, url]
    assert report.errors == ()
    assert report.retries == 1
    assert report.new == 1
    assert sleeps.calls == [5.0]  # backoff_s * k for k=1 (the one retry actually needed)


def test_run_sweep_retries_twice_after_503_503_then_succeeds(tmp_path):
    log = MessageLog(tmp_path)
    fetcher = _QueueFetcher()
    url = "https://fake.technocore.test/r/room-a?format=json&limit=200"
    fetcher.queue(url, FetchResponse(503, "text/plain", b"nope", False))
    fetcher.queue(url, FetchResponse(503, "text/plain", b"nope", False))
    fetcher.queue(
        url,
        FetchResponse(200, "application/json", _payload_bytes("room-a", 1, 1, [_msg(1)]), False),
    )
    sleeps = _SleepRecorder()

    report = message_log.run_sweep(
        ["room-a"], log=log, fetch=fetcher, clock=_FakeClock(), sleep=sleeps,
        interval_s=0.0, base_url="https://fake.technocore.test", retries=2, retry_backoff_s=1.0,
    )
    assert fetcher.calls == [url, url, url]
    assert report.errors == ()
    assert report.retries == 2
    assert sleeps.calls == [1.0, 2.0]  # backoff_s * 1, then backoff_s * 2


def test_run_sweep_exhausts_retries_with_identical_url_and_records_attempt_count(tmp_path):
    log = MessageLog(tmp_path)
    fetcher = _QueueFetcher()
    url = "https://fake.technocore.test/r/room-a?format=json&limit=200"
    for _ in range(3):
        fetcher.queue(url, FetchResponse(503, "text/plain", b"nope", False))

    report = message_log.run_sweep(
        ["room-a"], log=log, fetch=fetcher, clock=_FakeClock(), sleep=_no_sleep,
        interval_s=0.0, base_url="https://fake.technocore.test", retries=2, retry_backoff_s=0.1,
    )
    assert fetcher.calls == [url, url, url]
    assert dict(report.errors) == {"room-a": "http 503 after 3 attempts"}
    assert report.retries == 2
    assert report.new == 0


def test_run_sweep_non_5xx_429_status_is_recorded_at_once_without_retry(tmp_path):
    log = MessageLog(tmp_path)
    fetcher = _QueueFetcher()
    url = "https://fake.technocore.test/r/room-a?format=json&limit=200"
    fetcher.queue(url, FetchResponse(404, "text/plain", b"missing", False))

    report = message_log.run_sweep(
        ["room-a"], log=log, fetch=fetcher, clock=_FakeClock(), sleep=_no_sleep,
        interval_s=0.0, base_url="https://fake.technocore.test", retries=2, retry_backoff_s=5.0,
    )
    assert fetcher.calls == [url]  # exactly one request: 404 is never retried
    assert dict(report.errors) == {"room-a": "http 404"}
    assert report.retries == 0


def test_run_sweep_with_retries_zero_records_a_transport_failure_after_one_attempt(tmp_path):
    log = MessageLog(tmp_path)
    fetcher = _QueueFetcher()
    url = "https://fake.technocore.test/r/room-a?format=json&limit=200"
    fetcher.queue(url, TimeoutError("timed out"))

    report = message_log.run_sweep(
        ["room-a"], log=log, fetch=fetcher, clock=_FakeClock(), sleep=_no_sleep,
        interval_s=0.0, base_url="https://fake.technocore.test", retries=0, retry_backoff_s=5.0,
    )
    assert fetcher.calls == [url]
    assert dict(report.errors) == {"room-a": "TimeoutError: timed out after 1 attempts"}
    assert report.retries == 0


def test_run_sweep_negative_retries_is_treated_as_zero_and_never_raises(tmp_path):
    # regression: range(retries + 1) is empty for retries < 0, which used to fall through to
    # `_fetch_with_retries`'s "unreachable" AssertionError instead of making the one request.
    log = MessageLog(tmp_path)
    fetcher = _QueueFetcher()
    url = "https://fake.technocore.test/r/room-a?format=json&limit=200"
    fetcher.queue(
        url,
        FetchResponse(200, "application/json", _payload_bytes("room-a", 1, 1, [_msg(1)]), False),
    )

    report = message_log.run_sweep(
        ["room-a"], log=log, fetch=fetcher, clock=_FakeClock(), sleep=_no_sleep,
        interval_s=0.0, base_url="https://fake.technocore.test", retries=-1, retry_backoff_s=5.0,
    )
    assert fetcher.calls == [url]  # exactly one request, same as retries=0
    assert report.errors == ()
    assert report.retries == 0
    assert report.new == 1


def test_run_sweep_negative_retries_records_failure_after_one_attempt(tmp_path):
    log = MessageLog(tmp_path)
    fetcher = _QueueFetcher()
    url = "https://fake.technocore.test/r/room-a?format=json&limit=200"
    fetcher.queue(url, TimeoutError("timed out"))

    report = message_log.run_sweep(
        ["room-a"], log=log, fetch=fetcher, clock=_FakeClock(), sleep=_no_sleep,
        interval_s=0.0, base_url="https://fake.technocore.test", retries=-5, retry_backoff_s=5.0,
    )
    assert fetcher.calls == [url]
    assert dict(report.errors) == {"room-a": "TimeoutError: timed out after 1 attempts"}
    assert report.retries == 0


def test_run_sweep_negative_backoff_is_treated_as_zero_and_still_retries(tmp_path):
    # `time.sleep` refuses a negative argument; a bad --retry-backoff must not abort the sweep.
    log = MessageLog(tmp_path)
    fetcher = _QueueFetcher()
    url = "https://fake.technocore.test/r/room-a?format=json&limit=200"
    fetcher.queue(url, TimeoutError("timed out"))
    fetcher.queue(
        url,
        FetchResponse(200, "application/json", _payload_bytes("room-a", 1, 1, [_msg(1)]), False),
    )
    slept: list[float] = []

    report = message_log.run_sweep(
        ["room-a"], log=log, fetch=fetcher, clock=_FakeClock(), sleep=slept.append,
        interval_s=0.0, base_url="https://fake.technocore.test", retries=1, retry_backoff_s=-3.0,
    )
    assert fetcher.calls == [url, url]
    assert slept == [0.0]
    assert report.errors == ()
    assert report.retries == 1
    assert report.new == 1


def test_run_sweep_timeout_s_default_and_override_are_passed_to_the_fetcher(tmp_path):
    log = MessageLog(tmp_path)
    recorded: list[float] = []

    def _recording_fetch(
        url: str, timeout_s: float, max_bytes: int, user_agent: str
    ) -> FetchResponse:
        recorded.append(timeout_s)
        body = _payload_bytes("room-a", 1, 1, [_msg(1)])
        return FetchResponse(200, "application/json", body, False)

    message_log.run_sweep(
        ["room-a"], log=log, fetch=_recording_fetch, clock=_FakeClock(), sleep=_no_sleep,
        interval_s=0.0, base_url="https://fake.technocore.test",
    )
    assert recorded == [45.0]  # run_sweep's own default, matching main()'s --timeout default

    recorded.clear()
    log2 = MessageLog(tmp_path)
    message_log.run_sweep(
        ["room-a"], log=log2, fetch=_recording_fetch, clock=_FakeClock(), sleep=_no_sleep,
        interval_s=0.0, base_url="https://fake.technocore.test", timeout_s=7.0,
    )
    assert recorded == [7.0]


def test_cli_timeout_flag_flows_to_the_fetcher_default_then_override(tmp_path, monkeypatch):
    recorded: list[float] = []

    def _recording_fetch(
        url: str, timeout_s: float, max_bytes: int, user_agent: str
    ) -> FetchResponse:
        recorded.append(timeout_s)
        body = _payload_bytes("room-a", 1, 1, [_msg(1)])
        return FetchResponse(200, "application/json", body, False)

    monkeypatch.setattr(message_log, "_fetch_json", _recording_fetch)

    exit_code = message_log.main([
        "--root", str(tmp_path / "out1"), "--rooms-jsonl", str(tmp_path / "unused.jsonl"),
        "--room", "room-a", "--top", "0", "--once", "--interval", "0",
    ])
    assert exit_code == 0
    assert recorded == [45.0]  # main()'s own --timeout default

    recorded.clear()
    exit_code = message_log.main([
        "--root", str(tmp_path / "out2"), "--rooms-jsonl", str(tmp_path / "unused.jsonl"),
        "--room", "room-a", "--top", "0", "--once", "--interval", "0", "--timeout", "7",
    ])
    assert exit_code == 0
    assert recorded == [7.0]


# ============================================================= 5a-exclude. --exclude flag


def test_exclude_flag_drops_a_top_n_candidate_from_the_loopback_requests(tmp_path):
    rooms_path = tmp_path / "rooms.jsonl"
    rows = [
        {"id": "events", "message_count_seen": 500, "last_activity_ts": 0},
        {"id": "builders", "message_count_seen": 10, "last_activity_ts": 0},
    ]
    with rooms_path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")

    server = _Server({
        "/r/builders?format=json&limit=200": _payload_bytes("builders", 1, 1, [_msg(1)]),
    })
    try:
        root = tmp_path / "out"
        exit_code = message_log.main([
            "--root", str(root), "--rooms-jsonl", str(rooms_path),
            "--top", "1", "--exclude", "events", "--once", "--interval", "0",
            "--active-within-days", "999999999", "--base-url", server.base,
        ])
        assert exit_code == 0
        assert server.seen == ["/r/builders?format=json&limit=200"]  # never events
    finally:
        server.stop()


def test_room_and_exclude_same_id_exits_2_with_json_error_and_no_request(tmp_path, capsys):
    exit_code = message_log.main([
        "--root", str(tmp_path), "--rooms-jsonl", str(tmp_path / "rooms.jsonl"),
        "--room", "x", "--exclude", "x", "--top", "0", "--once",
    ])
    assert exit_code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    stderr_lines = [line for line in captured.err.splitlines() if line.strip()]
    assert len(stderr_lines) == 1
    error = json.loads(stderr_lines[0])
    assert set(error) == {"error"}
    assert not (tmp_path / "message-log-state.json").exists()


# ==================================================================== 6a. bad --room exits 2


def test_main_with_a_private_room_via_dash_dash_room_exits_2_with_json_error(tmp_path, capsys):
    exit_code = message_log.main([
        "--root", str(tmp_path), "--rooms-jsonl", str(tmp_path / "rooms.jsonl"),
        "--room", "p-secret", "--top", "0", "--once",
    ])
    assert exit_code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    stderr_lines = [line for line in captured.err.splitlines() if line.strip()]
    assert len(stderr_lines) == 1
    error = json.loads(stderr_lines[0])
    assert set(error) == {"error"}
    assert "p-secret" in error["error"]
    # no network was ever attempted, and nothing was written under --root
    assert not (tmp_path / "message-log-state.json").exists()


def test_main_with_default_top_and_missing_rooms_jsonl_exits_1_with_json_error(tmp_path, capsys):
    # Default --top is 20, so with no --room given select_rooms() must read --rooms-jsonl. A
    # missing/unreadable path raises FileNotFoundError (an OSError, not a ValueError) -- this
    # must come out as the documented single JSON error line and exit 1 (main()'s own
    # docstring: "any other unexpected exception exits 1 with the same error-line shape"), never
    # as a raw traceback.
    missing = tmp_path / "does-not-exist.jsonl"
    exit_code = message_log.main([
        "--root", str(tmp_path), "--rooms-jsonl", str(missing), "--once",
    ])
    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    stderr_lines = [line for line in captured.err.splitlines() if line.strip()]
    assert len(stderr_lines) == 1
    error = json.loads(stderr_lines[0])
    assert set(error) == {"error"}
    assert "FileNotFoundError" in error["error"]
    # nothing was written under --root besides the directory main() itself creates
    assert not (tmp_path / "message-log-state.json").exists()


# ============================================================== loopback-server scenarios


class _RoomPageHandler(BaseHTTPRequestHandler):
    """Serves `routes` (exact `path?query` -> JSON body bytes); records every requested path."""

    routes: dict = {}
    seen: list = []

    def log_message(self, *args):  # silence
        pass

    def do_GET(self):
        type(self).seen.append(self.path)
        body = self.routes.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _Server:
    def __init__(self, routes: dict) -> None:
        _RoomPageHandler.routes = routes
        _RoomPageHandler.seen = []
        self.httpd = HTTPServer(("127.0.0.1", 0), _RoomPageHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"

    @property
    def seen(self) -> list:
        return _RoomPageHandler.seen

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


# ================================================================ 5b. --once over a real socket


def test_once_over_loopback_server_reports_json_and_writes_the_two_files(tmp_path):
    server = _Server({
        "/r/room-a?format=json&limit=200": _payload_bytes(
            "room-a", 1, 2, [_msg(1, text="hello"), _msg(2, text="world")]
        ),
        "/r/room-b?format=json&limit=200": _payload_bytes("room-b", 1, 1, [_msg(1, text="hi")]),
    })
    try:
        root = tmp_path / "out"
        exit_code = message_log.main([
            "--root", str(root), "--rooms-jsonl", str(tmp_path / "unused-rooms.jsonl"),
            "--room", "room-a", "--room", "room-b", "--top", "0", "--once", "--interval", "0",
            "--base-url", server.base,
        ])
        assert exit_code == 0
        assert sorted(server.seen) == sorted([
            "/r/room-a?format=json&limit=200", "/r/room-b?format=json&limit=200",
        ])
        # exactly one request per room
        assert server.seen.count("/r/room-a?format=json&limit=200") == 1
        assert server.seen.count("/r/room-b?format=json&limit=200") == 1

        assert (root / "message-log-state.json").exists()
        assert (root / "messages" / "room-a.jsonl").exists()
        assert (root / "messages" / "room-b.jsonl").exists()

        log = MessageLog(root)
        assert log.last_seq("room-a") == 2
        assert log.last_seq("room-b") == 1
    finally:
        server.stop()


def test_once_over_loopback_server_stdout_is_one_compact_json_report_line(tmp_path, capsys):
    server = _Server({
        "/r/solo-room?format=json&limit=200": _payload_bytes(
            "solo-room", 1, 1, [_msg(1, text="only message")]
        ),
    })
    try:
        exit_code = message_log.main([
            "--root", str(tmp_path / "out"), "--rooms-jsonl", str(tmp_path / "unused.jsonl"),
            "--room", "solo-room", "--top", "0", "--once", "--interval", "0",
            "--base-url", server.base,
        ])
        assert exit_code == 0
        captured = capsys.readouterr()
        stdout_lines = [line for line in captured.out.splitlines() if line.strip()]
        assert len(stdout_lines) == 1
        report = json.loads(stdout_lines[0])
        assert report == {
            "rooms": 1, "new": 1, "duplicates": 0, "gaps": 0, "retries": 0, "errors": {},
            "seconds": report["seconds"],
        }
        assert isinstance(report["seconds"], (int, float))
    finally:
        server.stop()


def test_report_json_line_has_retries_key_between_gaps_and_errors(tmp_path, capsys):
    server = _Server({
        "/r/solo-room?format=json&limit=200": _payload_bytes(
            "solo-room", 1, 1, [_msg(1, text="only message")]
        ),
    })
    try:
        exit_code = message_log.main([
            "--root", str(tmp_path / "out"), "--rooms-jsonl", str(tmp_path / "unused.jsonl"),
            "--room", "solo-room", "--top", "0", "--once", "--interval", "0",
            "--base-url", server.base,
        ])
        assert exit_code == 0
        captured = capsys.readouterr()
        stdout_lines = [line for line in captured.out.splitlines() if line.strip()]
        assert len(stdout_lines) == 1
        keys = json.loads(stdout_lines[0], object_pairs_hook=lambda pairs: [k for k, _ in pairs])
        assert keys == ["rooms", "new", "duplicates", "gaps", "retries", "errors", "seconds"]
    finally:
        server.stop()


# =================================================== 6b. --loop + pre-existing STOP file


def test_loop_with_preexisting_stop_file_exits_zero_after_exactly_one_sweep(tmp_path, capsys):
    server = _Server({
        "/r/room-a?format=json&limit=200": _payload_bytes(
            "room-a", 1, 1, [_msg(1, text="only ever fetched once")]
        ),
    })
    try:
        root = tmp_path / "out"
        root.mkdir()
        (root / "STOP").write_text("", encoding="utf-8")

        exit_code = message_log.main([
            "--root", str(root), "--rooms-jsonl", str(tmp_path / "unused.jsonl"),
            "--room", "room-a", "--top", "0", "--loop", "--sleep", "0", "--max-runtime", "9999",
            "--interval", "0", "--base-url", server.base,
        ])
        assert exit_code == 0
        assert server.seen == ["/r/room-a?format=json&limit=200"]  # exactly one request, ever

        captured = capsys.readouterr()
        stdout_lines = [line for line in captured.out.splitlines() if line.strip()]
        assert len(stdout_lines) == 1  # exactly one sweep's report was printed
        report = json.loads(stdout_lines[0])
        assert report["rooms"] == 1
        assert report["new"] == 1
    finally:
        server.stop()


# ================================================== 8. --rooms-from-liveness (package LM2)


def _write_liveness(path: Path, rooms: dict, *, schema: str = "openagentsearch.liveness/1") -> None:
    body = {"schema": schema, "generated_at": "2026-09-19T08:30:00Z", "rooms": rooms}
    path.write_text(json.dumps(body), encoding="utf-8")


def _room_entry(room_class: str) -> dict:
    return {"class": room_class, "class_all": room_class, "signals": {}, "decided_on": [], "facts": {}}


def test_rooms_from_liveness_selects_included_classes_sorted_and_skips_bad_ids(tmp_path):
    path = tmp_path / "liveness-v1.json"
    _write_liveness(path, {
        "zeta": _room_entry("live"),
        "alpha": _room_entry("mixed"),
        "quiet-one": _room_entry("quiet"),
        "farm-one": _room_entry("farm"),
        "flood-one": _room_entry("flood"),
        "unknown-one": _room_entry("unknown"),
        "p-secret": _room_entry("live"),          # private: skipped, never selected
        "bad id!": _room_entry("live"),           # malformed id: skipped
        "no-class": {"facts": {}},                # no class: skipped
        "odd-class": _room_entry("shouting"),     # outside the vocabulary: skipped
    })
    result = message_log.rooms_from_liveness(path, ("live", "mixed", "quiet"))
    assert result.error is None
    assert result.rooms == ("alpha", "quiet-one", "zeta")
    assert result.skipped == 4
    assert result.generated_at == "2026-09-19T08:30:00Z"
    only_live = message_log.rooms_from_liveness(path, ("live",))
    assert only_live.rooms == ("zeta",)


def test_rooms_from_liveness_accepts_the_compact_schema_too(tmp_path):
    path = tmp_path / "liveness-compact.json"
    _write_liveness(path, {"r1": _room_entry("live")}, schema="openagentsearch.liveness-compact/1")
    assert message_log.rooms_from_liveness(path, ("live",)).rooms == ("r1",)


def test_rooms_from_liveness_never_raises_on_a_missing_or_malformed_map(tmp_path):
    missing = message_log.rooms_from_liveness(tmp_path / "nope.json", ("live",))
    assert missing.rooms == () and missing.error is not None and "unreadable" in missing.error

    not_json = tmp_path / "not.json"
    not_json.write_bytes(b"{not json")
    broken = message_log.rooms_from_liveness(not_json, ("live",))
    assert broken.rooms == () and broken.error is not None and "unreadable" in broken.error

    wrong_schema = tmp_path / "wrong.json"
    _write_liveness(wrong_schema, {"r1": _room_entry("live")}, schema="something/else")
    schema = message_log.rooms_from_liveness(wrong_schema, ("live",))
    assert schema.rooms == () and schema.error is not None and "schema" in schema.error

    no_rooms = tmp_path / "norooms.json"
    no_rooms.write_text(
        json.dumps({"schema": "openagentsearch.liveness/1", "rooms": []}), encoding="utf-8"
    )
    shapeless = message_log.rooms_from_liveness(no_rooms, ("live",))
    assert shapeless.rooms == () and shapeless.error is not None and "rooms" in shapeless.error

    oversize = tmp_path / "big.json"
    oversize.write_bytes(b"[" + b" " * (message_log.LIVENESS_MAX_BYTES + 1) + b"]")
    big = message_log.rooms_from_liveness(oversize, ("live",))
    assert big.rooms == () and big.error is not None and "oversize" in big.error


def test_parse_include_classes_validates_against_the_vocabulary():
    assert message_log.parse_include_classes("live,mixed,quiet") == ("live", "mixed", "quiet")
    assert message_log.parse_include_classes(" live , farm ") == ("live", "farm")
    with pytest.raises(ValueError, match="shouting"):
        message_log.parse_include_classes("live,shouting")
    with pytest.raises(ValueError):
        message_log.parse_include_classes(" , ")


def test_main_polls_map_rooms_after_explicit_minus_exclude_and_reports_liveness(tmp_path, capsys):
    liveness_path = tmp_path / "liveness-v1.json"
    _write_liveness(liveness_path, {
        "map-live": _room_entry("live"),
        "map-quiet": _room_entry("quiet"),
        "map-farm": _room_entry("farm"),
        "map-flood": _room_entry("flood"),
        "explicit-a": _room_entry("live"),      # also given via --room: deduplicated, explicit order kept
        "dropped": _room_entry("live"),         # also given via --exclude: never requested
    })
    server = _Server({
        "/r/explicit-a?format=json&limit=200": _payload_bytes("explicit-a", 1, 1, [_msg(1)]),
        "/r/map-live?format=json&limit=200": _payload_bytes("map-live", 1, 1, [_msg(1)]),
        "/r/map-quiet?format=json&limit=200": _payload_bytes("map-quiet", 1, 1, [_msg(1)]),
    })
    try:
        exit_code = message_log.main([
            "--root", str(tmp_path / "out"), "--rooms-jsonl", str(tmp_path / "unused.jsonl"),
            "--room", "explicit-a", "--top", "0", "--exclude", "dropped",
            "--rooms-from-liveness", str(liveness_path),
            "--once", "--interval", "0", "--base-url", server.base,
        ])
        assert exit_code == 0
        assert server.seen == [
            "/r/explicit-a?format=json&limit=200",
            "/r/map-live?format=json&limit=200",
            "/r/map-quiet?format=json&limit=200",
        ]
    finally:
        server.stop()
    out_lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(out_lines) == 1
    report = json.loads(out_lines[0])
    assert list(report) == [
        "rooms", "new", "duplicates", "gaps", "retries", "errors", "seconds", "liveness",
    ]
    assert report["rooms"] == 3
    assert report["liveness"] == {
        "rooms": 4, "skipped": 0, "error": None, "generated_at": "2026-09-19T08:30:00Z",
    }


def test_main_with_a_missing_map_still_polls_explicit_rooms_and_names_the_error(tmp_path, capsys):
    server = _Server({
        "/r/explicit-a?format=json&limit=200": _payload_bytes("explicit-a", 1, 1, [_msg(1)]),
    })
    try:
        exit_code = message_log.main([
            "--root", str(tmp_path / "out"), "--rooms-jsonl", str(tmp_path / "unused.jsonl"),
            "--room", "explicit-a", "--top", "0",
            "--rooms-from-liveness", str(tmp_path / "does-not-exist.json"),
            "--once", "--interval", "0", "--base-url", server.base,
        ])
        assert exit_code == 0
        assert server.seen == ["/r/explicit-a?format=json&limit=200"]
    finally:
        server.stop()
    report = json.loads(capsys.readouterr().out.strip())
    assert report["rooms"] == 1
    assert report["liveness"]["rooms"] == 0
    assert report["liveness"]["error"] is not None and "unreadable" in report["liveness"]["error"]
    assert report["liveness"]["generated_at"] is None


def test_main_without_the_flag_keeps_the_report_line_free_of_a_liveness_key(tmp_path, capsys):
    server = _Server({
        "/r/room-a?format=json&limit=200": _payload_bytes("room-a", 1, 1, [_msg(1)]),
    })
    try:
        exit_code = message_log.main([
            "--root", str(tmp_path / "out"), "--rooms-jsonl", str(tmp_path / "unused.jsonl"),
            "--room", "room-a", "--top", "0", "--once", "--interval", "0",
            "--base-url", server.base,
        ])
        assert exit_code == 0
    finally:
        server.stop()
    report = json.loads(capsys.readouterr().out.strip())
    assert "liveness" not in report


def test_main_with_a_bad_include_class_exits_2_with_json_error_and_no_request(tmp_path, capsys):
    liveness_path = tmp_path / "liveness-v1.json"
    _write_liveness(liveness_path, {"r1": _room_entry("live")})
    exit_code = message_log.main([
        "--root", str(tmp_path), "--rooms-jsonl", str(tmp_path / "rooms.jsonl"),
        "--room", "x", "--top", "0", "--once",
        "--rooms-from-liveness", str(liveness_path), "--include-classes", "live,shouting",
    ])
    assert exit_code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads([line for line in captured.err.splitlines() if line.strip()][0])
    assert set(error) == {"error"}
    assert "shouting" in error["error"]
    assert not (tmp_path / "message-log-state.json").exists()
