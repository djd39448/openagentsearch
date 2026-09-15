"""Package A4: `openagentsearch.pipeline.crawlconfig` -- the crawl config loader."""

from pathlib import Path

import pytest
import yaml

from openagentsearch.fetch.allowlist import AllowlistEntry
from openagentsearch.pipeline.crawlconfig import (
    CrawlConfig,
    GitHubDocsSource,
    GitHubIssuesSource,
    HostRule,
    allowlist_entries,
    config_sha256,
    load_crawl_config,
)

REPO = Path(__file__).resolve().parents[1]
COMMIT = "a" * 40


def _base_doc() -> dict:
    return {
        "hosts": {
            "flop.finance": {"max_pages": 60},
            "raw.githubusercontent.com": {"max_pages": 100, "path_prefixes": ["/flop-labs/"]},
        },
        "seeds": ["https://flop.finance/intro/"],
        "sources": {
            "rooms_jsonl": None,
            "github_docs": [{"owner": "flop-labs", "repo": "yellowpaper", "commit": COMMIT}],
            "github_issues": [{"owner": "flop-labs", "repo": "yellowpaper"}],
            "site_pages": ["https://flop.finance/intro/"],
        },
    }


def _write(tmp_path: Path, doc: dict) -> Path:
    path = tmp_path / "cfg.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------------- the real file


def test_committed_flop_yaml_loads():
    config = load_crawl_config(REPO / "config" / "flop.yaml")
    assert {h.host for h in config.hosts} == {
        "raw.githubusercontent.com", "flop.finance", "technocore.chat",
    }
    assert config.rooms_jsonl is None
    assert len(config.github_docs) == 4
    assert len(config.github_issues) == 2
    assert config.site_pages == ("https://flop.finance/intro/",)
    for seed in config.seeds:
        assert seed.startswith("https://flop.finance/")
    for gd in config.github_docs:
        assert len(gd.commit) == 40


def test_committed_flop_yaml_entries_map_to_allowlist():
    config = load_crawl_config(REPO / "config" / "flop.yaml")
    entries = allowlist_entries(config)
    assert AllowlistEntry("flop.finance", 60) in entries
    assert AllowlistEntry("technocore.chat", 25) in entries
    assert AllowlistEntry("raw.githubusercontent.com", 100) in entries


# ------------------------------------------------------------------------------------- validation


def test_bad_commit_raises_with_field_named(tmp_path):
    doc = _base_doc()
    doc["sources"]["github_docs"][0]["commit"] = "not-hex"
    path = _write(tmp_path, doc)
    with pytest.raises(ValueError, match="commit"):
        load_crawl_config(path)


def test_short_commit_raises(tmp_path):
    doc = _base_doc()
    doc["sources"]["github_docs"][0]["commit"] = "a" * 39
    path = _write(tmp_path, doc)
    with pytest.raises(ValueError, match="commit"):
        load_crawl_config(path)


def test_uppercase_commit_raises(tmp_path):
    doc = _base_doc()
    doc["sources"]["github_docs"][0]["commit"] = "A" * 40
    path = _write(tmp_path, doc)
    with pytest.raises(ValueError, match="commit"):
        load_crawl_config(path)


def test_bad_prefix_raises_with_field_named(tmp_path):
    doc = _base_doc()
    doc["hosts"]["raw.githubusercontent.com"]["path_prefixes"] = ["flop-labs/"]  # missing leading /
    path = _write(tmp_path, doc)
    with pytest.raises(ValueError, match="path_prefixes"):
        load_crawl_config(path)


def test_seed_on_non_allowlisted_host_raises_with_field_named(tmp_path):
    doc = _base_doc()
    doc["seeds"] = ["https://not-allowlisted.example/page"]
    path = _write(tmp_path, doc)
    with pytest.raises(ValueError, match="seeds"):
        load_crawl_config(path)


def test_seed_that_is_not_https_raises(tmp_path):
    doc = _base_doc()
    doc["seeds"] = ["http://flop.finance/intro/"]  # allowlisted host, but not https
    path = _write(tmp_path, doc)
    with pytest.raises(ValueError, match="seeds"):
        load_crawl_config(path)


def test_max_pages_must_be_a_positive_int(tmp_path):
    doc = _base_doc()
    doc["hosts"]["flop.finance"]["max_pages"] = 0
    path = _write(tmp_path, doc)
    with pytest.raises(ValueError, match="max_pages"):
        load_crawl_config(path)

    doc2 = _base_doc()
    doc2["hosts"]["flop.finance"]["max_pages"] = -5
    path2 = _write(tmp_path, doc2)
    with pytest.raises(ValueError, match="max_pages"):
        load_crawl_config(path2)


def test_host_must_be_lowercase(tmp_path):
    doc = _base_doc()
    doc["hosts"]["FLOP.finance"] = doc["hosts"].pop("flop.finance")
    doc["seeds"] = []
    path = _write(tmp_path, doc)
    with pytest.raises(ValueError, match="lowercase"):
        load_crawl_config(path)


def test_non_loopback_ip_literal_host_is_rejected(tmp_path):
    doc = _base_doc()
    doc["hosts"] = {"10.0.0.5": {"max_pages": 5}}
    doc["seeds"] = []
    path = _write(tmp_path, doc)
    with pytest.raises(ValueError, match="127.0.0.1"):
        load_crawl_config(path)


def test_loopback_ip_literal_host_is_accepted(tmp_path):
    doc = _base_doc()
    doc["hosts"] = {"127.0.0.1": {"max_pages": 5}}
    doc["seeds"] = []
    path = _write(tmp_path, doc)
    config = load_crawl_config(path)
    assert config.hosts == (HostRule("127.0.0.1", 5, ()),)


def test_owner_repo_must_match_name_pattern(tmp_path):
    doc = _base_doc()
    doc["sources"]["github_docs"][0]["owner"] = "bad owner"
    path = _write(tmp_path, doc)
    with pytest.raises(ValueError, match="owner"):
        load_crawl_config(path)

    doc2 = _base_doc()
    doc2["sources"]["github_issues"][0]["repo"] = "bad/repo"
    path2 = _write(tmp_path, doc2)
    with pytest.raises(ValueError, match="repo"):
        load_crawl_config(path2)


def test_hosts_must_be_a_non_empty_mapping(tmp_path):
    doc = _base_doc()
    doc["hosts"] = {}
    doc["seeds"] = []
    path = _write(tmp_path, doc)
    with pytest.raises(ValueError, match="hosts"):
        load_crawl_config(path)


def test_top_level_must_be_a_mapping(tmp_path):
    path = tmp_path / "cfg.yaml"
    path.write_text(yaml.safe_dump([1, 2, 3]), encoding="utf-8")
    with pytest.raises(ValueError):
        load_crawl_config(path)


def test_rooms_jsonl_string_is_accepted(tmp_path):
    doc = _base_doc()
    doc["sources"]["rooms_jsonl"] = "rooms.jsonl"
    path = _write(tmp_path, doc)
    config = load_crawl_config(path)
    assert config.rooms_jsonl == "rooms.jsonl"


# ------------------------------------------------------------------------------ dataclass identity


def test_host_rule_duplicate_hosts_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        CrawlConfig(
            hosts=(HostRule("a.test", 1), HostRule("a.test", 2)),
            seeds=(), rooms_jsonl=None, github_docs=(), github_issues=(), site_pages=(),
        )


def test_crawl_config_is_frozen_and_hashable_fields():
    config = CrawlConfig(
        hosts=(HostRule("a.test", 1, ("/x",)),),
        seeds=(), rooms_jsonl=None,
        github_docs=(GitHubDocsSource("o", "r", COMMIT),),
        github_issues=(GitHubIssuesSource("o", "r"),),
        site_pages=(),
    )
    with pytest.raises(Exception):
        config.hosts = ()  # type: ignore[misc]


def test_config_sha256_is_deterministic_and_sensitive_to_seeds_and_max_pages():
    a = CrawlConfig(
        hosts=(HostRule("a.test", 3),), seeds=("https://a.test/x",), rooms_jsonl=None,
        github_docs=(), github_issues=(), site_pages=(),
    )
    b = CrawlConfig(
        hosts=(HostRule("a.test", 3),), seeds=("https://a.test/x",), rooms_jsonl=None,
        github_docs=(), github_issues=(), site_pages=(),
    )
    assert config_sha256(a) == config_sha256(b)

    c = CrawlConfig(
        hosts=(HostRule("a.test", 5),), seeds=("https://a.test/x",), rooms_jsonl=None,
        github_docs=(), github_issues=(), site_pages=(),
    )
    assert config_sha256(a) != config_sha256(c)  # max_pages differs

    d = CrawlConfig(
        hosts=(HostRule("a.test", 3),), seeds=("https://a.test/y",), rooms_jsonl=None,
        github_docs=(), github_issues=(), site_pages=(),
    )
    assert config_sha256(a) != config_sha256(d)  # seeds differ


def test_loopback_seed_is_accepted_for_the_test_host_only():
    # http (not https) is allowed exactly when the seed's host is the reserved test loopback
    # literal -- the crawl loop's own done-when test seeds a plain http.server on 127.0.0.1.
    config = CrawlConfig(
        hosts=(HostRule("127.0.0.1", 3),), seeds=("http://127.0.0.1/p1.html",),
        rooms_jsonl=None, github_docs=(), github_issues=(), site_pages=(),
    )
    assert config.seeds == ("http://127.0.0.1/p1.html",)

    with pytest.raises(ValueError, match="seeds"):
        CrawlConfig(
            hosts=(HostRule("a.test", 3),), seeds=("http://a.test/p1.html",),
            rooms_jsonl=None, github_docs=(), github_issues=(), site_pages=(),
        )
