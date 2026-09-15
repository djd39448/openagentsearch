"""Package A3: pure helpers shared by every source adapter -- `split_markdown_sections` and
`slugify_heading` -- plus `SourceDoc`/`AdapterStats` validation."""

import pytest

from openagentsearch.sources.base import (
    AdapterStats,
    SourceDoc,
    slugify_heading,
    split_markdown_sections,
)

FENCED_MD = (
    "Intro paragraph before any heading, deliberately padded so it clears the "
    "min_section_chars merge threshold on its own without needing to borrow from a later "
    "section, since it has no earlier part to merge into anyway regardless of its length.\n\n"
    "## Real Heading One\n"
    "Some body text under the first real heading, long enough on its own that it will not "
    "be merged into anything else during this test even before the fenced block below.\n"
    "```\n"
    "## not a heading\n"
    "this fenced line must not split anything, ever, no matter what it looks like\n"
    "```\n"
    "More body text after the fenced block, still inside heading one's section here.\n"
    "### Tiny\n"
    "x\n"
    "## Real Heading Two\n"
    "Body under the second real heading, also padded out long enough on its own to clear "
    "the merge threshold without any help from neighboring sections in this fixture text.\n"
)


# 1. Headings inside fenced code blocks are never split points -------------------------------


def test_headings_inside_fenced_code_are_ignored():
    parts = split_markdown_sections(FENCED_MD, min_section_chars=50)
    headings = [h for h, _ in parts]
    assert "not a heading" not in headings
    assert "Real Heading One" in headings
    assert "Real Heading Two" in headings
    fenced_body = next(body for h, body in parts if h == "Real Heading One")
    assert "## not a heading" in fenced_body


# 2. A part shorter than min_section_chars merges into the previous part ---------------------


def test_small_parts_merge_into_previous_part():
    parts = split_markdown_sections(FENCED_MD, min_section_chars=50)
    headings = [h for h, _ in parts]
    assert "Tiny" not in headings
    absorbing = next(body for h, body in parts if h == "Real Heading One")
    assert "### Tiny" in absorbing and "\nx\n" in absorbing


def test_the_first_part_is_never_merged_even_when_tiny():
    tiny_leading = "hi\n## Heading\n" + ("padding " * 40) + "\n"
    parts = split_markdown_sections(tiny_leading, min_section_chars=50)
    assert parts[0] == (None, "hi\n")


# 3. A part longer than max_section_chars is cut at the last newline before the limit --------


def test_huge_part_is_cut_at_a_newline_boundary():
    lines = [f"line {i} of a very long section with enough padding text\n" for i in range(400)]
    huge = "## Huge\n" + "".join(lines)
    parts = split_markdown_sections(huge, max_section_chars=500)
    huge_parts = [(h, body) for h, body in parts if h == "Huge"]
    assert len(huge_parts) > 1
    for _, body in huge_parts[:-1]:
        assert len(body) <= 500
        assert body.endswith("\n")  # the cut landed on a full line, not mid-line
    assert sum(len(body) for _, body in huge_parts) == len(huge)


# 4. Total characters are always preserved ----------------------------------------------------


def test_total_characters_preserved():
    assert sum(len(b) for _, b in split_markdown_sections(FENCED_MD, min_section_chars=50)) == len(
        FENCED_MD
    )
    limited = split_markdown_sections(FENCED_MD, max_section_chars=120, min_section_chars=10)
    assert sum(len(b) for _, b in limited) == len(FENCED_MD)
    assert sum(len(b) for _, b in split_markdown_sections("")) == 0
    assert split_markdown_sections("") == [(None, "")]


# 5. Deterministic ------------------------------------------------------------------------------


def test_deterministic():
    a = split_markdown_sections(FENCED_MD, min_section_chars=50)
    b = split_markdown_sections(FENCED_MD, min_section_chars=50)
    assert a == b


# 6. slugify_heading ------------------------------------------------------------------------


def test_slugify_heading_basic():
    assert slugify_heading("Getting Started") == "getting-started"
    assert slugify_heading("Hello, World!") == "hello-world"
    assert slugify_heading("  Spaced   Out  ") == "spaced-out"


# 7. SourceDoc validation -------------------------------------------------------------------


def test_source_doc_accepts_a_well_formed_instance():
    doc = SourceDoc(
        url="https://example.test/a",
        kind="room",
        content="hello",
        content_type="text",
        fetched_at=1700000000.0,
        provenance=(("a", "1"), ("b", "2")),
        title="Title",
        section=None,
    )
    assert doc.provenance_dict() == {"a": "1", "b": "2"}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"url": ""},
        {"kind": "Bad-Kind"},
        {"content_type": "json"},
        {"content": ""},
        {"fetched_at": float("nan")},
        {"provenance": (("dup", "1"), ("dup", "2"))},
    ],
)
def test_source_doc_rejects_malformed_fields(kwargs):
    base = dict(
        url="https://example.test/a",
        kind="room",
        content="hello",
        content_type="text",
        fetched_at=1700000000.0,
        provenance=(("a", "1"),),
    )
    base.update(kwargs)
    with pytest.raises(ValueError):
        SourceDoc(**base)


def test_adapter_stats_is_a_plain_typed_tuple_of_counts():
    stats = AdapterStats(
        read=6, yielded=2, skipped_malformed=2, skipped_filtered=2, skipped_too_large=0
    )
    assert stats.read == 6 and stats.yielded == 2
