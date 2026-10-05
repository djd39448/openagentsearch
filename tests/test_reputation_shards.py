"""Package LS: the published, sharded form of the full ledger (`openagentsearch.reputation.shards`)
-- byte-exact split/join, deterministic output, the week-to-day fallback, the per-shard limit,
tamper detection, the atomic directory swap, and `reputation.build --shards-out`. Uses the 20-DID
reputation fixture."""

import gzip
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from reputation_fixture_support import NOW, install_fixture

from openagentsearch.reputation.ledger import build_ledger, to_jsonl_bytes
from openagentsearch.reputation.shards import (
    INDEX_NAME,
    SCHEMA_SHARDS,
    ShardSizeError,
    join_shards,
    split_ledger,
    write_shards,
)

REPO = Path(__file__).resolve().parents[1]


def _ledger_bytes(tmp_path: Path) -> bytes:
    ledger, _report = build_ledger(install_fixture(tmp_path / "log"), now=NOW)
    return to_jsonl_bytes(ledger)


def test_split_then_join_is_byte_exact_and_deterministic(tmp_path: Path):
    data = _ledger_bytes(tmp_path)
    out = tmp_path / "did-ledger"
    index = write_shards(data, out)
    assert index["schema"] == SCHEMA_SHARDS
    assert join_shards(out) == data
    assert split_ledger(data) == split_ledger(data)
    rows = sum(e["rows"] for e in index["shards"])
    assert rows == index["header"]["dids"] == 20
    for entry in index["shards"]:
        raw = gzip.decompress((out / entry["name"]).read_bytes())
        lines = raw.split(b"\n")[:-1]
        dids = [json.loads(line)["did"] for line in lines]
        assert dids == sorted(dids)  # ledger order inside a shard
        assert entry["key"].startswith("20") and entry["name"] == f"{entry['key']}.jsonl.gz"


def test_week_over_the_limit_is_split_by_day_and_still_joins(tmp_path: Path):
    data = _ledger_bytes(tmp_path)
    weekly = split_ledger(data)
    largest_week = max(len(v) for k, v in weekly.items() if k != INDEX_NAME)
    files = split_ledger(data, max_shard_bytes=largest_week - 1)
    index = json.loads(files[INDEX_NAME])
    assert any("split_from" in e for e in index["shards"])
    out = tmp_path / "split"
    write_shards(data, out, max_shard_bytes=largest_week - 1)
    assert join_shards(out) == data


def test_a_day_over_the_limit_raises_before_anything_is_written(tmp_path: Path):
    data = _ledger_bytes(tmp_path)
    out = tmp_path / "never"
    with pytest.raises(ShardSizeError):
        write_shards(data, out, max_shard_bytes=10)
    assert not out.exists()


@pytest.mark.parametrize("tamper", ["edit", "delete", "extra", "index"])
def test_join_refuses_a_tampered_directory(tmp_path: Path, tamper: str):
    data = _ledger_bytes(tmp_path)
    out = tmp_path / "did-ledger"
    index = write_shards(data, out)
    first = out / index["shards"][0]["name"]
    if tamper == "edit":
        raw = gzip.decompress(first.read_bytes()).replace(b'"post_count":', b'"post_count":9', 1)
        first.write_bytes(gzip.compress(raw, mtime=0))
    elif tamper == "delete":
        first.unlink()
    elif tamper == "extra":
        (out / "1999-W01.jsonl.gz").write_bytes(gzip.compress(b"", mtime=0))
    else:
        obj = json.loads((out / INDEX_NAME).read_bytes())
        obj["header"]["dids"] = 19
        (out / INDEX_NAME).write_text(json.dumps(obj), encoding="utf-8")
    with pytest.raises(ValueError):
        join_shards(out)


def test_write_replaces_the_directory_and_drops_stale_shards(tmp_path: Path):
    data = _ledger_bytes(tmp_path)
    out = tmp_path / "did-ledger"
    out.mkdir()
    (out / "stale.jsonl.gz").write_bytes(b"x")
    write_shards(data, out)
    assert not (out / "stale.jsonl.gz").exists()
    assert join_shards(out) == data
    assert not list(tmp_path.glob(".did-ledger*"))  # no staging or .old directory left behind


def test_build_cli_writes_shards_that_join_to_its_own_out(tmp_path: Path):
    root = install_fixture(tmp_path / "log")
    out = tmp_path / "out" / "did-ledger.jsonl"
    shards = tmp_path / "out" / "did-ledger"
    env = dict(os.environ, PYTHONPATH=str(REPO / "src"))
    proc = subprocess.run(
        [sys.executable, "-m", "openagentsearch.reputation.build", "--log-root", str(root),
         "--out", str(out), "--now", str(NOW), "--shards-out", str(shards)],
        cwd=str(REPO), env=env, capture_output=True, text=True, timeout=120, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report["shards"]["dids"] == 20 and report["shards"]["ledger_bytes"] == out.stat().st_size
    assert join_shards(shards) == out.read_bytes()
