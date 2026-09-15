OpenAgentSearch static index export
schema: openagentsearch.static-index/1

manifest.json - every document the pipeline has attempted to index, one row per
  doc_sha256, with its status (indexed / failed / superseded / refused) and reason.
flop-surface.jsonl - one JSON object per line, only for rows whose status is "indexed";
  "abstract" is a lexical cut of the extracted text, never a summary.

Both are GET-only static artifacts: plain files an operator regenerates and publishes
on their own schedule. No freshness is guaranteed and neither file is signed.

"superseded" documents stay in manifest.json but are excluded from
flop-surface.jsonl -- only the current indexed document per source_url appears there.

Nothing here is a ranking, a recommendation, or an endorsement of any listed page.

Once published, these are intended to be served at:
  https://djd39448.github.io/openagentsearch/index/manifest.json
  https://djd39448.github.io/openagentsearch/index/flop-surface.jsonl
