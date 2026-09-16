"""Precomputed lexical (BM25) index: tokenizer, typed index + JSON form, builder, and the
reference ranking a JavaScript port (the Cloudflare Worker, package C2b) must reproduce exactly.

Standard library only. See `docs/static-index.md` ("lexical-v1.json") for the on-disk layout and
`handoff/C1-DESIGN.md` §2-3 for why the index is shaped this way.
"""
