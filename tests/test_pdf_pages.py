# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""PDF extraction boundaries and real scoped Memory retrieval."""

from __future__ import annotations

import asyncio
import hashlib
import io

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import (
    BuiltinConfig,
    GetMemoryEntryRequest,
    SubmitSourceObservation,
    open_builtin_runtime,
)
from powercontext.builtin.scope import ScopeDraft
from powercontext.sources import SourceDefinitionRegistry, manifest_for_definition, project_source_for_transport
from powercontext.sources.pdf_pages import (
    PDF_PAGE_SOURCE_DEFINITION,
    PDFDocumentInput,
    PDFPageInput,
    parse_pdf_document,
    search_pdf_pages,
)
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY


def pdf(texts, password=None):
    writer = PdfWriter()
    for text in texts:
        page = writer.add_blank_page(width=400, height=600)
        if text:
            font = DictionaryObject({
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            })
            page[NameObject("/Resources")] = DictionaryObject({
                NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})
            })
            stream = DecodedStreamObject()
            escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            stream.set_data(f"BT /F1 12 Tf 20 500 Td ({escaped}) Tj ET".encode())
            page[NameObject("/Contents")] = stream
    if password is not None:
        writer.encrypt(user_password=password, owner_password="owner")  # noqa: S106
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def test_all_pages_retain_words_numbers_boundaries_and_exact_digest():
    content = pdf(["Revenue 123.45 expenses 67.89", "", "Operating income 9876"])
    pages = parse_pdf_document(PDFDocumentInput(document_id="report", content=content))
    assert [(v.page_number, v.text.strip()) for v in pages] == [
        (1, "Revenue 123.45 expenses 67.89"),
        (2, ""),
        (3, "Operating income 9876"),
    ]
    assert {v.document_sha256 for v in pages} == {hashlib.sha256(content).hexdigest()}


@pytest.mark.parametrize(
    "changes",
    [
        {"document_id": " \t"},
        {"document_sha256": "A" * 64},
        {"document_sha256": "0" * 63},
        {"page_number": 0},
        {"page_number": True},
        {"page_number": 1.5},
    ],
)
def test_direct_pages_reject_invalid_identity(changes):
    with pytest.raises(ValueError):
        PDFPageInput(**({"document_id": "report", "document_sha256": "a" * 64, "page_number": 1, "text": ""} | changes))


def test_password_and_truncated_final_page_fail_without_partial_result():
    with pytest.raises(ValueError):
        parse_pdf_document(PDFDocumentInput(document_id="locked", content=pdf(["first", "last"], password="secret")))  # noqa: S106
    content = pdf(["first", "last"])
    with pytest.raises(ValueError):
        parse_pdf_document(PDFDocumentInput(document_id="broken", content=content[:-50]))


def test_identity_distinguishes_document_revision_and_page_and_round_trips():
    async def scenario():
        registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
        original = PDFPageInput(
            document_id=" 文档:/" * 100, document_sha256="a" * 64, page_number=1, text="Revenue 123"
        )
        sources = [
            await registry.resolve(page)
            for page in (
                original,
                original.model_copy(update={"document_id": "other"}),
                original.model_copy(update={"document_sha256": "b" * 64}),
                original.model_copy(update={"page_number": 2}),
            )
        ]
        assert len({v.name for v in sources}) == 4
        assert all(len(v.name) <= 256 for v in sources)
        source = sources[0]
        restored = type(source).model_validate_json(source.model_dump_json())
        assert await registry.read(restored) == original
        assert (
            project_source_for_transport(registry, restored).projection(TEXT_EVIDENCE_PROJECTION_KEY)["content"]
            == original.document_id + "\nRevenue 123"
        )

    asyncio.run(scenario())


def test_retrieval_uses_real_memory_distinct_pages_and_scope_after_restart(tmp_path):
    class Pipeline:
        async def extract(self, request, /):
            entries = []
            for source in request.sources:
                text = source.projection(TEXT_EVIDENCE_PROJECTION_KEY)["content"]
                entries.append(MemoryEntryInput(kind="page", text=text, sources=(source,)))
                if "Capital expenditures" in text:
                    entries.append(
                        MemoryEntryInput(
                            kind="duplicate-reference", text="Capital expenditures spending capex", sources=(source,)
                        )
                    )
            return tuple(entries)

    async def scenario():
        registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'pdf.db'}"))
        inputs = parse_pdf_document(
            PDFDocumentInput(
                document_id="ACME_2025_10K",
                content=pdf([
                    "Capital expenditures for new factories total 128 million.",
                    "Revenue from customers was 512 million. Net sales increased.",
                    "Litigation and legal proceedings remain unresolved.",
                ]),
            )
        )
        observations = [project_source_for_transport(registry, await registry.resolve(page)) for page in inputs]
        async with open_builtin_runtime(config, candidate_pipeline=Pipeline()) as runtime:
            scope = await runtime.scopes.create(
                ScopeDraft(title="Reports", summary="Evidence", idempotency_key="reports")
            )
            other = await runtime.scopes.create(
                ScopeDraft(title="Private", summary="Other scope", idempotency_key="private")
            )
            await runtime.ingestion.register(manifest_for_definition(PDF_PAGE_SOURCE_DEFINITION))
            for observation in observations:
                await runtime.ingestion.submit(
                    SubmitSourceObservation(scope_id=scope.scope_id, observation=observation)
                )
            competing_page = PDFPageInput(
                document_id="OTHER_2025_10K",
                document_sha256="b" * 64,
                page_number=1,
                text="Capital expenditures spending capex factories 999 million.",
            )
            competing = project_source_for_transport(registry, await registry.resolve(competing_page))
            await runtime.ingestion.submit(SubmitSourceObservation(scope_id=scope.scope_id, observation=competing))
            private_page = PDFPageInput(
                document_id="private", document_sha256="a" * 64, page_number=1, text="secret scope unique phrase"
            )
            private = project_source_for_transport(registry, await registry.resolve(private_page))
            await runtime.ingestion.submit(SubmitSourceObservation(scope_id=other.scope_id, observation=private))
            for scope_id in (scope.scope_id, other.scope_id):
                while (await runtime.memory.for_scope(scope_id).flush()).processed:
                    pass
        async with open_builtin_runtime(config) as runtime:
            memory = runtime.memory.for_scope(scope.scope_id)
            for question, expected in [
                ("What was ACME capex in 2025?", 1),
                ("How much revenue did ACME earn?", 2),
                ("What lawsuits are pending?", 3),
            ]:
                citations = await search_pdf_pages(memory, question, limit=1)
                assert len(citations) == 1
                entry = (await memory.get(GetMemoryEntryRequest(citation=citations[0]))).entry
                assert entry.sources[0].source_id == observations[expected - 1].name
            citations = await search_pdf_pages(memory, "capital expenditures capex spending", limit=20)
            locations = [
                (await memory.get(GetMemoryEntryRequest(citation=c))).entry.sources[0].source_id for c in citations
            ]
            assert len(set(locations)) == len(locations) == 2
            assert set(locations) == {observations[0].name, competing.name}
            assert await search_pdf_pages(memory, "secret scope unique phrase") == ()
            assert await search_pdf_pages(memory, "capital", limit=0) == ()
            assert await search_pdf_pages(memory, "   ") == ()
            with pytest.raises(ValueError):
                await search_pdf_pages(memory, "capital", limit=-1)

    asyncio.run(scenario())
