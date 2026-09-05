# Agent API Contract

This document specifies the machine-readable contract for OpenAgentSearch's agent-facing API endpoints.

## GET /search

### Parameters

| Name | In | Type | Required | Default | Constraints | Notes |
|------|----|------|----------|---------|-------------|-------|
| q | query | string | Yes | None | `max_length` 512, `non_empty_after_strip` True | Query string, maximum 512 characters. Preserved verbatim when accepted. |
| k | query | integer | No | 10 | `min` 1, `max` 50 | Number of results to return, default 10. |

### Success fields

- `query`: The original query string.
- `k`: The number of results requested.
- `results`: List of result objects.

#### Result object fields:
- `chunk_id`: Unique identifier for the chunk.
- `doc_sha256`: SHA256 hash of the document.
- `doc_url`: URL of the document (string or null).
- `score`: Cosine similarity score.
- `snippet`: First 200 characters of the stored chunk text.

### Nested fields

- `results`: List of result objects.

### Errors

| Status | Error Code | When |
|--------|------------|------|
| 400 | missing_query | Missing required parameter q or q is empty after stripping. |
| 400 | query_too_long | Query parameter q exceeds 512 characters. |
| 400 | invalid_k | Parameter k is not a valid number or out of range [1, 50]. |

### Contract JSON

```json
{"params": {"k": {"constraints": {"max": 50, "min": 1}, "default": 10, "in": "query", "notes": "Number of results to return, default 10.", "required": false, "type": "integer"}, "q": {"constraints": {"max_length": 512, "non_empty_after_strip": true}, "default": null, "in": "query", "notes": "Query string, maximum 512 characters. Preserved verbatim when accepted.", "required": true, "type": "string"}}, "success": {"keys": ["query", "k", "results"], "nested": {"results": {"keys": ["chunk_id", "doc_sha256", "doc_url", "score", "snippet"]}}, "status": 200}, "errors": [{"error": "missing_query", "status": 400, "when": "Missing required parameter q or q is empty after stripping."}, {"error": "query_too_long", "status": 400, "when": "Query parameter q exceeds 512 characters."}, {"error": "invalid_k", "status": 400, "when": "Parameter k is not a valid number or out of range [1, 50]."}]}
```

## GET /doc/{doc_sha256}

### Parameters

| Name | In | Type | Required | Default | Constraints | Notes |
|------|----|------|----------|---------|-------------|-------|
| doc_sha256 | path | string | Yes | None | `pattern` [0-9a-f]{{64}}, `case` lower | Exactly 64 lowercase ASCII hex characters. |

### Success fields

- `doc_sha256`: SHA256 hash passed in the URL.
- `url`: The original URL of the fetched document.
- `title`: Title derived from the document.
- `lang`: Language code.
- `text`: The full text extracted from the document.
- `extracted_at`: Timestamp when the document was extracted.
- `provenance`: Source information about how the document was fetched (or null).

### Nested fields

- `provenance`: Object containing source details, or null.

### Errors

| Status | Error Code | When |
|--------|------------|------|
| 400 | invalid_sha256 | Path parameter doc_sha256 is not exactly 64 lowercase ASCII hex characters. |
| 404 | not_found | Document with the given SHA256 hash does not exist in the system. |

### Contract JSON

```json
{"params": {"doc_sha256": {"constraints": {"case": "lower", "pattern": "[0-9a-f]{64}"}, "default": null, "in": "path", "notes": "Exactly 64 lowercase ASCII hex characters.", "required": true, "type": "string"}}, "success": {"keys": ["doc_sha256", "url", "title", "lang", "text", "extracted_at", "provenance"], "nested": {"provenance": {"keys": ["url", "fetched_at", "status", "sha256", "robots_allowed"]}}, "status": 200}, "errors": [{"error": "invalid_sha256", "status": 400, "when": "Path parameter doc_sha256 is not exactly 64 lowercase ASCII hex characters."}, {"error": "not_found", "status": 404, "when": "Document with the given SHA256 hash does not exist in the system."}]}
```