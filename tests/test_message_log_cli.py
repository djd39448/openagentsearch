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


class _QueueFetcher:
    """Records every URL requested (in order) and answers from a per-URL FIFO queue. Raises if a
    URL is requested that nothing queued -- catches a query-string mistake immediately rather
    than hanging or returning a confusing empty body."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._queues: dict[str, list[FetchResponse]] = {}

    def queue(self, url: str, response: FetchResponse) -> None:
        self._queues.setdefault(url, []).append(response)

    def __call__(
        self, url: str, timeout_s: float, max_bytes: int, user_agent: str
    ) -> FetchResponse:
        self.calls.append(url)
        pending = self._queues.get(url)
        if not pending:
            raise AssertionError(f"unexpected URL requested (nothing queued): {url}")
        return pending.pop(0)


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
            "rooms": 1, "new": 1, "duplicates": 0, "gaps": 0, "errors": {},
            "seconds": report["seconds"],
        }
        assert isinstance(report["seconds"], (int, float))
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
