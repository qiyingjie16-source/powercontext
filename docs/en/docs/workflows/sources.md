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
