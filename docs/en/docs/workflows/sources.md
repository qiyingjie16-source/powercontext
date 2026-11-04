---
title: Sources and capture
description: Capture evidence, inspect Source records, and configure processing.
---

# Sources and capture

Sources preserve evidence before it becomes selected knowledge. A Content Source stores captured text; other Source
types can refer to material supplied by a connector. Work Contracts and Task Outcomes also preserve Source evidence.

## Capture and inspect

1. Choose an existing Scope. The [API Quick Start](../develop/api-quickstart.md) demonstrates content capture through
   `POST /v1/sources/content` and retains the returned Source reference.
2. Use `GET /v1/scopes/{scope_id}/sources` to browse the Source journal, then
   `GET /v1/scopes/{scope_id}/sources/{source_type}/{source_id}` to read the selected Source.
3. To process eligible evidence into Memory, configure a generation model and either scheduled processing or an
   explicit Memory flush. See [Enable extraction and vector search](../get-started/configure-models.md).

A successful capture means evidence was stored; it does not mean extraction succeeded or that the result was worth
keeping. Use Source references to trace a generated result back to its input. Do not retry a capture with changed
content under the same identity as if it were a new observation.

## Choose a capture path

- Agent prompt hooks: use the host-specific capture switch in [Connect Agents](../integrations/index.md).
- Files and object stores: use the [OpenDAL connector](ingest-text-files-with-opendal.md).
- Subject evidence for a Scope profile: follow [Scope profiles](use-profiles.md).
- Application or evaluation events: use the application's adapter; [Bub capture](../integrations/evaluation.md) is opt-in.

Source writes and reads are subject to the selected [Scope and access policy](scopes-and-access.md).

## Offline CSV record capture

`powercontext.sources.csv_rows` provides `CSVDocumentInput`, `parse_csv_document`, and
`CSV_ROW_SOURCE_DEFINITION`. Parse the complete UTF-8 document before submitting records.
Invalid encoding, headers, keys, widths, or CSV syntax raise `ValueError` without returning partial rows.
Headers and values retain their original strings; the primary-key column must match exactly.
Record identities separately percent-encode the document ID and key value and are limited to 256 characters.

Create a `SourceDefinitionRegistry((CSV_ROW_SOURCE_DEFINITION,))`, register
`manifest_for_definition(CSV_ROW_SOURCE_DEFINITION)` with `runtime.ingestion.register`, then resolve each row.
Use `project_source_for_transport(registry, source)` to build an observation and submit it with
`runtime.ingestion.submit(SubmitSourceObservation(scope_id=scope_id, observation=observation))` to an existing Scope.

Document parsing is atomic; individual submissions are not a document-wide database transaction.
The standard text projection contains compact JSON in original column order and complete record metadata.
Runtime retains observations and projections, so restart reads do not require the CSV file or worker Definition.
After Memory processing, construct an exact `MemoryCitation` from a search hit's `memory_ref`, `entry_id`,
and `entry_version_id` to retrieve the entry and its Source evidence.
Stable record identity does not permit overwriting captured content; conflicting submissions retain Runtime's conflict behavior.

## Pi session branch history

`powercontext.sources.pi_session` provides `PiSessionInput`, `parse_pi_session`,
`PI_EVENT_SOURCE_DEFINITION`, and `PiSessionConnector(path, leaf_id=None)`.
The parser validates the complete UTF-8/BOM JSONL v3 graph, selects the last node by default or an explicit node,
and returns visible raw events from root to leaf. Control nodes retain tree links without producing text.
Compaction and branch summaries do not replace earlier evidence. Full tool results remain in Sources independently of Memory text limits.

Resolve/read events with `SourceDefinitionRegistry((PI_EVENT_SOURCE_DEFINITION,))`; use
`manifest_for_definition` and `project_source_for_transport` with Runtime ingestion as for CSV.
Each identity is `pi:` followed by SHA-256 of the JSON array containing session ID and event ID (67 characters).
Sources retain complete original records; standard text evidence includes full text and event identity metadata.

Run the Connector through `ConnectorLifecycle` with binding name `pi-session-history`, version `1`,
and host-provided durable sink and checkpoint store. Each run reads and validates the complete file,
checks full-record JSON digests, and submits only unseen selected-branch events.
The opaque checkpoint accumulates imported event IDs and digests by session; JSON layout and key order do not affect equality.
Shared ancestors are deduplicated across branches. Previously captured branch evidence remains available;
rewriting or removing an imported record in the current session rejects the run.
Lifecycle commits progress after successful writes. Partial failures may leave durable Sources but preserve the old checkpoint;
retry uses Runtime's idempotent acceptance. Each run scans the complete archive and progress storage grows with imported history.

## Whole-document PDF page capture and retrieval

`powercontext.sources.pdf_pages` exposes `PDFDocumentInput`, `PDFPageInput`, `parse_pdf_document`,
`PDF_PAGE_SOURCE_DEFINITION`, and `search_pdf_pages`. Parsing uses pypdf, supplied by the fixed competition environment.
Pass the original complete PDF bytes. All physical pages, including blanks, are extracted in order without OCR,
text truncation, or merging neighboring pages. Empty-password permission PDFs are accepted; password-protected
or incompletely parseable documents fail with `ValueError` without returning partial pages.

Each page retains a nonblank document ID, SHA-256 of the entire original byte sequence, and one-based physical
page number, independent of printed page labels. Identity is `pdf:` plus SHA-256 of the JSON array containing
those three components. Revisions receive new identities and leave old references unchanged.
Standard text evidence contains `DOCUMENT_ID\nPAGE_TEXT` and exactly `document_id`, `document_sha256`, and
`page_number` metadata. Resolve pages through the Definition registry and submit manifests/observations through
Runtime ingestion as for CSV; then process Memory before searching.

`await search_pdf_pages(runtime.memory.for_scope(scope_id), question, limit=20)` returns exact Memory citations.
It uses offline FTS, general financial vocabulary expansion, and candidate-text BM25 with document-label matching.
Every returned citation is read back with `memory.get` and deduplicated by PDF Source identity.
No answers, evidence labels, file scans, synthetic references, module-level indexes, or cross-scope caches are used.
Empty questions, zero limits, and unmatched queries return an empty tuple. A page citation requires one PDF page
Source per Memory entry. Each search performs a main query and matching vocabulary-group queries, each requesting
`max(100, limit * 5)` candidates. Full extraction and valid citations do not establish evidence-page recall;
quality and Recall thresholds require a separate labeled evaluation.

## HTTP Memory reranking

`powercontext.builtin.artifacts.memory.http_reranker.HTTPMemoryReranker` accepts a scoring endpoint, a caller-owned `httpx.AsyncClient`, positive `batch_size`, and policy ID. Pass it through Runtime's `memory_reranker` parameter.

Each POST carries `query` and candidate objects with the original one-based `rank` and `text`. A response must contain exactly `scores`, with each score object containing exactly `rank` and a finite numeric `score`. Scores may be returned out of order, but each batch must cover its own ranks exactly once. Invalid schemas raise `ValueError`; HTTP status and network errors retain the original `httpx` exception.

After every batch has scored its candidates, ranks are sorted by descending score and then ascending original rank. `InferenceUsage.requests` reports the number of HTTP calls. The existing Memory service maps returned original ranks back to unchanged entry and version identities. Relevance quality depends on the scoring service.

## Full and incremental context packs

`powercontext.builtin.runtime.context_pack` provides `ContextPack`, `ContextPackDelta`, `apply_context_pack_delta`, and `ContextPackError`. `ScopedContextApplication.prepare_incremental(request, base_pack=None)` calls the existing `prepare` to obtain the target content. The first call returns a full pack; a call with a base returns a prefix delta. An empty prepared result becomes an empty pack.

Content is split deterministically at UTF-8 character boundaries, with a default maximum of 512 bytes per item. Wire objects use standard Base64 and sorted compact JSON. The SHA-256 digest covers only `schema` and `items`; `byte_count` includes the serialized field itself. Delta reconstruction uses only serialized base and delta values and validates both digests. Decoding rejects missing or extra fields, invalid Base64 or UTF-8, invalid prefixes, and digest or byte-count mismatches.

Transmission cost includes the first full pack and every later delta as `len(to_bytes())`, including Base64 and JSON overhead. The public tests check the protocol and one real Runtime incremental call; the complete 40-round cost threshold requires its own measurement sequence.

## Source count admission

`RuntimeConfig.memory_source_count_threshold` and `topic_memory_source_count_threshold` default to `None` and accept only strict positive integers when enabled. The global supervisor counts persisted Source journal identities after each scope and family's successful cursor. Duplicate capture and receipt trust upgrades do not add identities. Lineage-only Sources count toward admission while remaining excluded from generation evidence.

Count and time admission are independent alternatives. Explicit `flush()` accepts work directly. Memory consumes one bounded window and rechecks the remaining count after success; an accepted Topic Memory wave continues its existing window sequence. Accepted requests and successful cursors remain durable through failures and restart. Discovery scans bounded pages and checks again approximately one second after a full pass.

## Coalescing embedding requests

Pass `CoalescingEmbeddingModel` from `powercontext.builtin.inference.coalescing` as `embedding_model` to `open_builtin_contexts`. Calls on one event loop share one wrapper, exposing the underlying model's profile. Defaults are a 5 ms coalescing window, 64 unique texts per provider batch, and an LRU of 1024 successful vectors.

Duplicate texts share pending and running requests. Results preserve each caller's order and duplicates. Setting `cache_size=0` disables reuse across completed calls while retaining in-flight sharing. Failed batches and vector-count mismatches are not cached; independent batches continue. Caller cancellation leaves shared computation alive. Each caller aggregates provider request and token usage once per distinct awaited batch; cache-only calls report zero requests. The cache is local to the wrapper's process.
