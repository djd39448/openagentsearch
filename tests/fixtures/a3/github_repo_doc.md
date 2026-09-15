# Repo Docs Title

Intro paragraph before any level-2 or level-3 heading, describing what this fixture file is for
and giving the section splitter something reasonably sized to treat as the document's leading,
heading-less part before it reaches the first real heading below.

## Section One

Body content for section one, written long enough on its own to clear the two-hundred character
minimum-section-length merge threshold that `split_markdown_sections` applies with its default
parameters, so this part is never silently absorbed into whichever part happens to precede it in
this fixture file, no matter how the splitter is invoked by the adapter under test.

```text
## not a heading
This line lives inside a fenced code block and must never be treated as a markdown heading or
become the start of a new section, no matter what its own text looks like.
```

More section-one prose after the fence closes, still gathered under the same "Section One"
heading as everything above it, padded out with a second sentence so the whole part comfortably
clears the merge threshold even after the fenced block above it is counted too.

## Section Two

Body content for section two, likewise padded out long enough to stand on its own as a distinct
part rather than being merged into section one or section three, with enough filler text here to
comfortably clear the same two-hundred character minimum-section-length threshold on its own.

### Section Three

Body content for section three, a level-3 heading nested after two level-2 headings earlier in
this same fixture file, again padded out with enough filler text to clear the minimum-section
merge threshold so it survives as its own distinct part in the adapter's test assertions.
