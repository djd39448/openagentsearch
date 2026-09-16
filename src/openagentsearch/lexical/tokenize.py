"""The `unicode-word-casefold-v1` tokenizer: pure, deterministic, and defined character-class by
character-class (`unicodedata.category`, never `\\w`) specifically so that a JavaScript port can
reproduce it exactly with `\\p{L}` / `\\p{N}` regex classes -- the two implementations are held
identical by the shared fixture `tests/fixtures/lexical/tokenizer-vectors.json`.

What this module does NOT do: no stemming, no stopword removal, no language detection. Casefold
(not merely lowercase) and NFKC normalization are the only text transforms applied before word
splitting, so e.g. German "straße" and full-width digits fold the same way in every reader.
"""

import unicodedata

TOKENIZER_ID = "unicode-word-casefold-v1"

_MIN_TOKEN_LEN = 2
_MAX_TOKEN_LEN = 40

# Public so a caller that assembles text FOR tokenize() (e.g.
# openagentsearch.lexical.build.build_lexical_index, which concatenates title + section + the
# extracted body) can bound it to this same limit itself, rather than finding out only when
# tokenize() raises.
MAX_INPUT_CHARS = 1_000_000
_MAX_INPUT_CHARS = MAX_INPUT_CHARS


def _is_word_char(ch: str) -> bool:
    """A character belongs to a word run when it is `_` or its Unicode general category starts
    with `L` (letter) or `N` (number) -- the same partition `\\p{L}` / `\\p{N}` / `_` draws in a
    JS/ICU regex engine. Deliberately NOT `str.isalnum()` or `\\w`, both of which classify some
    codepoints (for example some `Nl`/`No` cases) differently across engines."""
    if ch == "_":
        return True
    category = unicodedata.category(ch)
    return category[0] in ("L", "N")


def _emit_run(run: str) -> list[str]:
    """One maximal word run (already NFKC-normalized and casefolded) -> the tokens it contributes:
    the run itself if its length is in `[_MIN_TOKEN_LEN, _MAX_TOKEN_LEN]`, followed by each of its
    `_`-separated parts that independently passes the same length rule -- except a part equal to
    the whole run (which cannot actually arise, since splitting on a `_` the run is known to
    contain always yields strictly shorter parts; the check is kept as a explicit no-repeat
    guarantee, not a workaround for an observed case)."""
    tokens: list[str] = []
    if _MIN_TOKEN_LEN <= len(run) <= _MAX_TOKEN_LEN:
        tokens.append(run)
    if "_" in run:
        for part in run.split("_"):
            if part == run:
                continue
            if _MIN_TOKEN_LEN <= len(part) <= _MAX_TOKEN_LEN:
                tokens.append(part)
    return tokens


def tokenize(text: str) -> list[str]:
    """`text` -> tokens, in order, duplicates preserved (term-frequency counting needs them).

    Pipeline: NFKC-normalize -> `str.casefold()` -> split into maximal runs of `_is_word_char`
    characters -> each run through `_emit_run`. Pure: no I/O, no randomness, no locale dependence
    beyond what `unicodedata`/`str.casefold()` themselves apply.

    Raises:
        ValueError: `text` is over 1,000,000 characters. This is the only bound enforced here --
            there is no bound on the number of tokens a within-limit input can produce.
    """
    if len(text) > _MAX_INPUT_CHARS:
        raise ValueError(
            f"text is {len(text)} characters, over the {_MAX_INPUT_CHARS}-character limit"
        )
    normalized = unicodedata.normalize("NFKC", text).casefold()

    tokens: list[str] = []
    run_start: int | None = None
    for i, ch in enumerate(normalized):
        if _is_word_char(ch):
            if run_start is None:
                run_start = i
        elif run_start is not None:
            tokens.extend(_emit_run(normalized[run_start:i]))
            run_start = None
    if run_start is not None:
        tokens.extend(_emit_run(normalized[run_start:]))
    return tokens


def query_terms(text: str, *, max_terms: int = 32) -> list[str]:
    """`tokenize(text)` reduced to distinct terms in first-seen order, cut at `max_terms`.

    NOT guaranteed: this says nothing about which terms exist in any particular index -- a term
    returned here may have no postings at all (see `openagentsearch.lexical.search.search`).
    """
    seen: set[str] = set()
    distinct: list[str] = []
    for token in tokenize(text):
        if token not in seen:
            seen.add(token)
            distinct.append(token)
    return distinct[:max_terms]
