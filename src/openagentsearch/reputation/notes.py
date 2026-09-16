"""The `did-*` note convention: an optional, LABELED, never-trusted way for a DID to claim a
GitHub login for itself.

A note is a JSON object `{"did": "...", "github_login": "...", "author": "..."}`, one per line in
a plain JSONL file. It is used for a given `did` ONLY when that same line's `"author"` field
equals `"did"` -- i.e. the DID wrote the claim about itself -- so this file can never be used to
plant a login for someone else. Nothing here fetches a note (there is no network in this
package); nothing here checks the claimed `github_login` against any real GitHub account either --
see `facts.DidFacts.github_login_source`, which labels every login this loader produces as
`"did-note (convention strength, unverified)"`, never as verified.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class NotesReport:
    """What one `load_did_notes()` call read. `lines` is every non-blank line in the file;
    `used` is how many of them became a `did -> github_login` entry; `skipped_malformed` counts
    lines that were not a JSON object, or were missing/mistyped `did`/`github_login`/`author`;
    `skipped_not_self_authored` counts otherwise-well-formed lines whose `author` did not equal
    their own `did` (a claim about a DID made by somebody else, silently ignored rather than
    trusted). `lines == used + skipped_malformed + skipped_not_self_authored` always holds. A
    `did` that appears more than once keeps only its LAST self-authored line (a later line
    overwrites an earlier one for the same `did`, same as the JSONL append-only convention used
    elsewhere in this repository)."""

    lines: int
    used: int
    skipped_malformed: int
    skipped_not_self_authored: int


def load_did_notes(path: Path | None) -> tuple[dict[str, str], NotesReport]:
    """Read the optional `did-*` note file at `path` (`None` -> an empty result, no error) and
    return `(did -> github_login, NotesReport)`.

    A line is used only when it parses as a JSON object carrying non-empty string `did`,
    `github_login` and `author` fields, AND `author == did` (the DID wrote the note about
    itself) -- every other line is skipped and counted, never raised on. This function makes no
    network call: fetching or verifying a note against a real account is explicitly a later
    package's job (see this module's own docstring and `docs/reputation.md`).

    NOT guaranteed: a self-authored claim is not verified against anything -- `author == did`
    only proves the line SAYS the DID wrote it, which this loader has no way to check against a
    real signature (the message log's `sig` field is a different thing entirely, checked
    nowhere in this package either -- see `facts.Post`).
    """
    if path is None:
        return {}, NotesReport(lines=0, used=0, skipped_malformed=0, skipped_not_self_authored=0)

    file_path = Path(path)
    if not file_path.is_file():
        return {}, NotesReport(lines=0, used=0, skipped_malformed=0, skipped_not_self_authored=0)

    result: dict[str, str] = {}
    lines = 0
    used = 0
    skipped_malformed = 0
    skipped_not_self_authored = 0
    with file_path.open("rb") as fh:
        for raw_line in fh:
            stripped = raw_line.strip()
            if not stripped:
                continue
            lines += 1
            try:
                obj: Any = json.loads(stripped.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                skipped_malformed += 1
                continue
            if not isinstance(obj, dict):
                skipped_malformed += 1
                continue
            did = obj.get("did")
            github_login = obj.get("github_login")
            author = obj.get("author")
            if (
                not isinstance(did, str)
                or not did
                or not isinstance(github_login, str)
                or not github_login
                or not isinstance(author, str)
                or not author
            ):
                skipped_malformed += 1
                continue
            if author != did:
                skipped_not_self_authored += 1
                continue
            result[did] = github_login
            used += 1

    report = NotesReport(
        lines=lines,
        used=used,
        skipped_malformed=skipped_malformed,
        skipped_not_self_authored=skipped_not_self_authored,
    )
    return result, report
