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
