"""Post-roadmap #5: the public documentation states facts the repository can back, and nothing more."""

import importlib
import re
import tomllib
from pathlib import Path

import openagentsearch

REPO = Path(__file__).resolve().parents[1]
FORBIDDEN_MACHINE_STRINGS = (
    "C:\\Users\\",
    "trustcore-rdp",
    "oas-pydeps",
    "uv-0.12.3",
    "cpython-3.12-windows",
    "AppData\\Roaming\\uv",
)
PUBLIC_DOCS = ("README.md", "docs/getting-started.md", "CONTRIBUTING.md", "CHANGELOG.md", "docs/releasing.md")


def _read(relative: str) -> str:
    return (REPO / relative).read_text(encoding="utf-8")


def _flat(text: str) -> str:
    """Collapse Markdown line wrapping so phrase checks are layout-independent."""
    return " ".join(text.split())


def test_version_sources_and_changelog_agree():
    with open(REPO / "pyproject.toml", "rb") as fh:
        pyproject_version = tomllib.load(fh)["project"]["version"]
    assert pyproject_version == openagentsearch.__version__ == "0.1.0"
    changelog = _read("CHANGELOG.md")
    assert "## 0.1.0 - Development baseline" in changelog
    flat = _flat(changelog)
    assert "No git tag or published release is asserted" in flat
    assert "does not imply that a git tag, package publication, or hosted release exists" in flat


def test_getting_started_names_real_public_symbols():
    doc = _read("docs/getting-started.md")
    flat = _flat(doc)
    symbols = {
        "index_document": ("openagentsearch.pipeline.index", "index_document"),
        "VectorStore": ("openagentsearch.vector.store", "VectorStore"),
        "cosine_search": ("openagentsearch.vector.search", "cosine_search"),
        "OllamaEmbedClient": ("openagentsearch.embed.ollama", "OllamaEmbedClient"),
        "create_server": ("openagentsearch.api.server", "create_server"),
        "make_search_route": ("openagentsearch.api.search", "make_search_route"),
        "make_doc_route": ("openagentsearch.api.doc", "make_doc_route"),
    }
    for name, (module_name, attribute) in symbols.items():
        assert name in doc, name
        assert getattr(importlib.import_module(module_name), attribute) is not None
    assert "openagentsearch.mcp.server" in doc
    assert importlib.import_module("openagentsearch.mcp.server") is not None
    assert "no HTTP server CLI" in flat
    assert "synthetic" in flat and "not production relevance ground truth" in flat


def test_public_docs_do_not_overstate_runtime_or_security():
    raw = "\n".join(_read(path) for path in PUBLIC_DOCS)
    flat = _flat(raw)
    required = (
        "no production index",
        "process-level",
        "not a security boundary",
        "not implemented",
        "no remote-provider fallback",
        "does not authorize a merge",
    )
    for phrase in required:
        assert phrase in flat, phrase
    for forbidden in FORBIDDEN_MACHINE_STRINGS:
        assert forbidden not in raw, forbidden


def test_roadmap_and_contributing_distinguish_status_from_policy():
    roadmap = _read("ROADMAP.md")
    flat_roadmap = _flat(roadmap)
    assert "## Implementation Status" in roadmap
    assert "Phases 0 through 6 below have been implemented in the repository" in flat_roadmap
    assert "does not mean the project is production-ready" in flat_roadmap
    for identifier in ("P0", "P1", "P2", "P3", "P4", "P5", "P6"):
        assert re.search(rf"\b{identifier}\.\d", roadmap), identifier
    contributing = _read("CONTRIBUTING.md")
    flat_contributing = _flat(contributing)
    assert "## Implemented gate status" in contributing
    assert "Not implemented:" in contributing
    assert "automated PR ingestion, GitHub merge automation" in flat_contributing
    assert "automated orchestrator sign-off" in flat_contributing
    assert "Sandbox success does not authorize a merge." in flat_contributing


def test_release_document_has_no_tag_or_publish_side_effect_commands():
    releasing = _read("docs/releasing.md")
    assert "## Release checklist" in releasing
    assert releasing.count("- [ ]") >= 5
    for forbidden in ("git tag", "git push --tags", "twine upload", "hatch publish", "uv publish", "gh release create"):
        assert forbidden not in releasing, forbidden
    assert "owner-authorized" in releasing
