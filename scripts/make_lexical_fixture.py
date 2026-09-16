"""Regenerates the two committed lexical-index fixtures package C2a tests replay against:

  tests/fixtures/lexical/fixture-index-v1.json -- a LexicalIndex over 40 synthetic documents
    (4 source kinds x 10 docs each), built the same way pipeline.publish would build one.
  tests/fixtures/lexical/queries.json -- 20 queries against that index, each recorded with the
    ranked hits `openagentsearch.lexical.search.search` (the reference ranking a JS port must
    match) actually produces for it.

Deterministic: every document's content, URL and manifest timestamp is a pure function of its
(kind, index) position, and GENERATED_AT is a fixed constant rather than "now" -- so running this
script twice produces byte-identical output both times (checked by this script itself, at the end,
before either file is written).

Usage: python scripts/make_lexical_fixture.py
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # scripts/ is not a package

from openagentsearch.extract.store import ExtractStore  # noqa: E402
from openagentsearch.fetch.rawstore import RawStore  # noqa: E402
from openagentsearch.index.manifest import ManifestEntry  # noqa: E402
from openagentsearch.lexical.build import build_lexical_index  # noqa: E402
from openagentsearch.lexical.index import to_json_bytes  # noqa: E402
from openagentsearch.lexical.search import search  # noqa: E402
from openagentsearch.vector.store import VectorStore  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "lexical"
GENERATED_AT = 1_700_000_000.0

# 10 topics per source_kind, each a single distinctive word so every doc has a term unique to it.
TOPICS: dict[str, tuple[str, ...]] = {
    "room": (
        "authentication", "caching", "logging", "pagination", "throttling",
        "retrying", "sharding", "timeouts", "validation", "webhooks",
    ),
    "github_doc": (
        "installation", "configuration", "deployment", "migration", "monitoring",
        "profiling", "scheduling", "serialization", "templating", "versioning",
    ),
    "github_issue": (
        "crash", "deadlock", "leak", "regression", "segfault",
        "timeout", "nullref", "race", "overflow", "invalidstate",
    ),
    "site": (
        "pricing", "onboarding", "dashboards", "integration", "notifications",
        "permissions", "subscriptions", "analytics", "compliance", "encryption",
    ),
}

# One underscore identifier per github_issue doc (index-aligned with TOPICS["github_issue"]) --
# exercises "a run containing `_` is emitted whole and as its `_`-separated parts" end to end.
ISSUE_IDENTIFIERS: tuple[str, ...] = (
    "startup_crash_report",
    "deadlock_detected_here",
    "memory_leak_confirmed",
    "regression_test_failure",
    "segmentation_fault_trace",
    "connection_timeout_error",
    "null_pointer_exception",
    "race_condition_found",
    "stack_overflow_error",
    "invalid_state_transition",
)

# "gateway": every doc (40/40 = 100% > 50%) -> always dropped by max_df_ratio.
# "review": all 10 room + all 10 site + github_doc[0] (21/40 = 52.5% > 50%) -> also dropped.
# "error": all 10 github_issue + site[0..4] (15/40 = 37.5%) -> kept, frequent enough for k-cuts.
# "integration": site[3] (its own topic word) and also mentioned in github_doc[2] -> a term that
#   spans two source_kinds, for the kind-filter queries.


def _doc_text(kind: str, index: int, topic: str) -> str:
    words = [
        f"{topic.capitalize()} overview.",
        f"This record discusses {topic} in the {kind} knowledge base, entry number {index}.",
        f"{topic.capitalize()} {topic} {topic} details follow for anyone routing through the "
        "gateway.",
    ]
    if kind == "github_issue":
        words.append(f"Identifier {ISSUE_IDENTIFIERS[index]} tracks this error report.")
    if kind in ("room", "site"):
        words.append("A review of this record is recorded here for completeness.")
    if kind == "site" and index < 5:
        words.append("An error was reported by a user and is noted here.")
    if kind == "github_doc" and index == 0:
        words.append("A review of this document was completed last quarter.")
    if kind == "github_doc" and index == 2:
        words.append("This page also covers integration with the deployment pipeline.")
    return " ".join(words)


def _build_fixture_store(root: Path) -> VectorStore:
    """Populate a fresh VectorStore + raw/extracted tree at `root` with 40 indexed documents,
    10 per TOPICS key, and return the (still-open) store."""
    store = VectorStore(root / "vectors.sqlite3", dimension=2)
    raw = RawStore(root)
    extracted = ExtractStore(root)

    indexed_at = GENERATED_AT - 1000.0
    for kind, topics in TOPICS.items():
        for index, topic in enumerate(topics):
            if kind == "github_doc":
                url = f"https://github.com/example/repo/blob/main/docs/{topic}.md#overview"
            elif kind == "github_issue":
                url = f"https://github.com/example/repo/issues/{100 + index}"
            elif kind == "room":
                url = f"https://chat.example.test/rooms/{topic}"
            else:
                url = f"https://example.test/site/{topic}"

            text = _doc_text(kind, index, topic)
            body = f"<html><body><h1>{topic}</h1><p>{text}</p></body></html>".encode("utf-8")
            sha256 = raw.put(url, body, 200, True, indexed_at)
            store.record_manifest(
                ManifestEntry(
                    doc_sha256=sha256,
                    source_url=url,
                    status="indexed",
                    reason="",
                    indexed_at=indexed_at,
                    chunk_count=1,
                    extracted_sha256=sha256,
                    source_kind=kind,
                )
            )
            extracted.put(
                sha256, url,
                {"text": text, "title": topic.capitalize(), "lang": "en"},
                indexed_at + 0.5,
            )
    return store


# The 20 golden queries: single term, multi-term, underscore identifier (whole and parts), kind
# filters, a no-hit term, a casefold-only variant of an earlier query, and k=3 cuts -- see
# handoff/C2a-SPEC.md deliverable item 7.
QUERIES: tuple[dict[str, object], ...] = (
    {"q": "authentication", "k": 5, "kind": None},
    {"q": "AUTHENTICATION", "k": 5, "kind": None},  # casefold-only difference vs. the row above
    {"q": "caching", "k": 5, "kind": None},
    {"q": "connection_timeout_error", "k": 5, "kind": None},  # underscore identifier, whole+parts
    {"q": "timeout error", "k": 5, "kind": None},  # multi-term
    {"q": "deployment integration", "k": 5, "kind": None},  # multi-term
    {"q": "integration", "k": 10, "kind": "site"},  # kind-filtered
    {"q": "integration", "k": 10, "kind": "github_doc"},  # kind-filtered, other kind
    {"q": "integration", "k": 10, "kind": None},  # same term, unfiltered
    {"q": "quantumfluxcapacitor", "k": 5, "kind": None},  # no-hit
    {"q": "error", "k": 3, "kind": None},  # k=3 cut (15 matching docs)
    {"q": "error", "k": 3, "kind": "site"},  # same term, kind-filtered to a lower-scoring kind
    # -- deliberately different top hits than the unfiltered row above, to prove the kind filter
    # actually narrows the candidate set rather than merely happening to agree with it.
    {"q": "memory_leak_confirmed", "k": 5, "kind": None},  # underscore identifier
    {"q": "null_pointer_exception", "k": 3, "kind": None},  # underscore identifier
    {"q": "pricing", "k": 5, "kind": None},
    {"q": "versioning", "k": 5, "kind": "github_doc"},
    {"q": "encryption compliance", "k": 5, "kind": "site"},  # multi-term, kind-filtered
    {"q": "webhooks retrying", "k": 5, "kind": "room"},  # multi-term, kind-filtered
    {"q": "gateway", "k": 5, "kind": None},  # dropped term (100% df) -> no-hit
    {"q": "review", "k": 5, "kind": None},  # dropped term (52.5% df) -> no-hit
)


def _build_and_serialize(tmp_dir: Path) -> tuple[bytes, list[dict[str, object]]]:
    store = _build_fixture_store(tmp_dir)
    try:
        index, _report = build_lexical_index(store=store, root=tmp_dir, generated_at=GENERATED_AT)
    finally:
        store.close()
    index_bytes = to_json_bytes(index)

    queries_out: list[dict[str, object]] = []
    for query in QUERIES:
        kind = query["kind"]
        assert kind is None or isinstance(kind, str)
        q = query["q"]
        k = query["k"]
        assert isinstance(q, str) and isinstance(k, int)
        hits = search(index, q, k, kind=kind)
        queries_out.append(
            {
                "q": q,
                "k": k,
                "kind": kind,
                "hits": [{"sha": hit.sha, "score": hit.score} for hit in hits],
            }
        )
    return index_bytes, queries_out


def main() -> int:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)

    # Self-check determinism before writing anything: two independent builds, in two independent
    # temp trees, must be byte-identical (index) and produce identical golden query results.
    with tempfile.TemporaryDirectory() as tmp1, tempfile.TemporaryDirectory() as tmp2:
        bytes1, queries1 = _build_and_serialize(Path(tmp1))
        bytes2, queries2 = _build_and_serialize(Path(tmp2))
    if bytes1 != bytes2:
        raise AssertionError("fixture-index-v1.json is not deterministic across two builds")
    if queries1 != queries2:
        raise AssertionError("queries.json is not deterministic across two builds")

    index_path = FIXTURES_DIR / "fixture-index-v1.json"
    index_path.write_bytes(bytes1)

    queries_path = FIXTURES_DIR / "queries.json"
    queries_path.write_text(
        json.dumps(queries1, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print(
        json.dumps(
            {
                "fixture_index_path": str(index_path),
                "fixture_index_bytes": len(bytes1),
                "queries_path": str(queries_path),
                "query_count": len(queries1),
            },
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
