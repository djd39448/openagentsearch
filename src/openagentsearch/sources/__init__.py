"""FLOP source adapters: pure, network-free readers of already-fetched or injected-fetch content
(technocore.chat's room directory, pinned-commit GitHub markdown and issues, and an explicit
operator-supplied list of site pages) that all produce the same `SourceDoc` shape for
`openagentsearch.pipeline.index.index_source_document(s)`. Every adapter takes its I/O (a file
path, an injected fetcher callable, or already-loaded JSON) as a constructor argument; none of
them opens a socket on its own."""
