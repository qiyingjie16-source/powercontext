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
import zlib

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, EncodedStreamObject, NameObject

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


@pytest.mark.parametrize("damage", ["truncated-flate", "missing-form"])
def test_corrupt_later_page_streams_fail_even_with_valid_document_trailer(damage):
    writer = PdfWriter()
    writer.append_pages_from_reader(PdfReader(io.BytesIO(pdf(["first valid page", "second page"]))))
    contents = writer.pages[1].get_contents()
    assert contents is not None
    text_program = contents.get_data()
    if damage == "truncated-flate":
        stream = EncodedStreamObject()
        stream[NameObject("/Filter")] = NameObject("/FlateDecode")
        stream._data = zlib.compress(text_program)[:-4]
    else:
        stream = DecodedStreamObject()
        stream.set_data(text_program + b"\n/Missing Do")
    writer.pages[1][NameObject("/Contents")] = stream
    buffer = io.BytesIO()
    writer.write(buffer)
    with pytest.raises(ValueError):
        parse_pdf_document(PDFDocumentInput(document_id="damaged", content=buffer.getvalue()))


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


def test_retrieval_uses_real_memory_distinct_pages_and_scope_after_restart(tmp_path):  # noqa: C901
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
                    "Consolidated Statement of Income",
                    "Consolidated Statement of Cash Flows",
                    "Notes to Financial Statements: income and cash flow disclosure, earnings and operations.",
                    "Our products are sold in more than 200 countries and geographic regions.",
                    "Inventories are valued using the LIFO method.",
                    "Income Taxes: The effective tax rate reconciles to the statutory rate.",
                    "Research and development and depreciation and amortization expense are disclosed together.",
                    "Current assets were 200 million and current liabilities were 100 million.",
                    "Accounts receivable were 50 million and net revenue was 500 million.",
                ]),
            )
        )
        observations = [project_source_for_transport(registry, await registry.resolve(page)) for page in inputs]
        async with open_builtin_runtime(config, candidate_pipeline=Pipeline()) as runtime:
            assert runtime.scopes is not None
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
            johnson_page = PDFPageInput(
                document_id="JOHNSON_JOHNSON_2025_10K",
                document_sha256="c" * 64,
                page_number=1,
                text="Material litigation and legal proceedings are disclosed by Johnson & Johnson.",
            )
            johnson = project_source_for_transport(registry, await registry.resolve(johnson_page))
            await runtime.ingestion.submit(SubmitSourceObservation(scope_id=scope.scope_id, observation=johnson))
            private_page = PDFPageInput(
                document_id="private", document_sha256="a" * 64, page_number=1, text="secret scope unique phrase"
            )
            private = project_source_for_transport(registry, await registry.resolve(private_page))
            await runtime.ingestion.submit(SubmitSourceObservation(scope_id=other.scope_id, observation=private))
            for scope_id in (scope.scope_id, other.scope_id):
                while (await runtime.memory.for_scope(scope_id).flush()).processed:
                    pass
        async with open_builtin_runtime(config, candidate_pipeline=Pipeline()) as runtime:
            memory = runtime.memory.for_scope(scope.scope_id)
            for question, expected in [
                ("What was ACME capex in 2025?", 1),
                ("How much revenue did ACME earn?", 2),
                ("What lawsuits are pending at ACME?", 3),
            ]:
                citations = await search_pdf_pages(memory, question, limit=1)
                assert len(citations) == 1
                entry = (await memory.get(GetMemoryEntryRequest(citation=citations[0]))).entry
                assert entry.sources[0].source_id == observations[expected - 1].name, question
            johnson_citations = await search_pdf_pages(memory, "What litigation does Johnson & Johnson face?", limit=1)
            assert len(johnson_citations) == 1
            johnson_entry = (await memory.get(GetMemoryEntryRequest(citation=johnson_citations[0]))).entry
            assert johnson_entry.sources[0].source_id == johnson.name
            citations = await search_pdf_pages(memory, "capital expenditures capex spending", limit=20)
            locations = [
                (await memory.get(GetMemoryEntryRequest(citation=c))).entry.sources[0].source_id for c in citations
            ]
            assert len(set(locations)) == len(locations) == 2
            assert set(locations) == {observations[0].name, competing.name}
            combined = await search_pdf_pages(
                memory, "Use ACME's income statement and statement of cash flows to estimate operating profit.", limit=2
            )
            assert {
                (await memory.get(GetMemoryEntryRequest(citation=citation))).entry.sources[0].source_id
                for citation in combined
            } == {observations[3].name, observations[4].name}
            for question, expected in [
                ("In which geographic regions does ACME operate?", 7),
                ("How does ACME value its inventories?", 8),
                ("What income tax rate applies?", 9),
                ("What were ACME R&D and D&A expenses?", 10),
            ]:
                citations = await search_pdf_pages(memory, question, limit=1)
                assert len(citations) == 1
                entry = (await memory.get(GetMemoryEntryRequest(citation=citations[0]))).entry
                assert entry.sources[0].source_id == observations[expected - 1].name
            quick_ratio = await search_pdf_pages(memory, "What is ACME's quick ratio?", limit=2)
            assert {
                (await memory.get(GetMemoryEntryRequest(citation=citation))).entry.sources[0].source_id
                for citation in quick_ratio
            } == {observations[10].name, observations[11].name}
            added_page = PDFPageInput(
                document_id="ACME_2025_10K",
                document_sha256="d" * 64,
                page_number=13,
                text="Our workforce includes 12,345 employees.",
            )
            added_source = await registry.resolve(added_page)
            await runtime.ingestion.submit(
                SubmitSourceObservation(
                    scope_id=scope.scope_id,
                    observation=project_source_for_transport(registry, added_source),
                )
            )
            while (await memory.flush()).processed:
                pass
            added = await search_pdf_pages(memory, "How large is ACME's workforce?", limit=1)
            assert len(added) == 1
            added_entry = (await memory.get(GetMemoryEntryRequest(citation=added[0]))).entry
            assert added_entry.sources[0].source_id == added_source.name
            assert await search_pdf_pages(memory, "secret scope unique phrase") == ()
            assert await search_pdf_pages(memory, "capital", limit=0) == ()
            assert await search_pdf_pages(memory, "   ") == ()
            with pytest.raises(ValueError):
                await search_pdf_pages(memory, "capital", limit=-1)

    asyncio.run(scenario())


def test_retrieval_routes_abbreviations_and_periods_and_keeps_identical_pages(tmp_path):
    class Pipeline:
        async def extract(self, request, /):
            return tuple(
                MemoryEntryInput(
                    kind="page", text=source.projection(TEXT_EVIDENCE_PROJECTION_KEY)["content"], sources=(source,)
                )
                for source in request.sources
            )

    async def scenario():
        registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'routing.db'}"))
        statement = "Consolidated Balance Sheets\nCurrent assets 240 current liabilities 120 cash 60 receivables 80."
        documents = (
            ("NORTHSTARTECHNOLOGIES_2024_10K", 1, statement),
            ("NORTHSTARTECHNOLOGIES_2025_10K", 1, statement),
            ("NORTHSTARTECHNOLOGIES_2025_10K", 2, statement),
            ("BETA_BETA_2025_10K", 1, statement),
            ("DECOY_2025_10K", 1, statement),
        )
        sources = tuple([
            await registry.resolve(
                PDFPageInput(document_id=document, document_sha256="a" * 64, page_number=page, text=text)
            )
            for document, page, text in documents
        ])
        async with open_builtin_runtime(config, candidate_pipeline=Pipeline()) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(
                ScopeDraft(title="Reports", summary="Reports", idempotency_key="reports")
            )
            await runtime.ingestion.register(manifest_for_definition(PDF_PAGE_SOURCE_DEFINITION))
            for source in sources:
                await runtime.ingestion.submit(
                    SubmitSourceObservation(
                        scope_id=scope.scope_id, observation=project_source_for_transport(registry, source)
                    )
                )
            while (await runtime.memory.for_scope(scope.scope_id).flush()).processed:
                pass
        async with open_builtin_runtime(config) as runtime:
            memory = runtime.memory.for_scope(scope.scope_id)
            for question, expected in (
                ("What is NSTE's current ratio for FY25?", {sources[1].name, sources[2].name}),
                ("What is BnB's current ratio for FY25?", {sources[3].name}),
                (
                    "Compare Northstar Technologies and Beta & Beta current ratios for FY25.",
                    {sources[1].name, sources[2].name, sources[3].name},
                ),
                ("What are the current ratios?", {source.name for source in sources}),
            ):
                citations = await search_pdf_pages(memory, question, limit=20)
                actual = {
                    (await memory.get(GetMemoryEntryRequest(citation=citation))).entry.sources[0].source_id
                    for citation in citations
                }
                assert actual == expected, question
                assert len(citations) == len(expected)

    asyncio.run(scenario())


def test_ratio_retrieval_preserves_complementary_statements_among_narrative_matches(tmp_path):
    class Pipeline:
        async def extract(self, request, /):
            return tuple(
                MemoryEntryInput(
                    kind="page", text=source.projection(TEXT_EVIDENCE_PROJECTION_KEY)["content"], sources=(source,)
                )
                for source in request.sources
            )

    async def scenario():
        registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'statements.db'}"))
        texts = [
            f"Capital-intensive business outlook {number}. Capital investment and business growth are important."
            for number in range(30)
        ] + [
            "Consolidated Statements of Income\nNet sales 900 800\nCost of sales 400 300\nNet income 200 180",
            "Consolidated Balance Sheets\nTotal assets 1200 1100\nProperty plant equipment 700 650\nEquity 500 450",
            "Consolidated Statements of Cash Flows\nOperating activities 300 250\nCapital expenditures 80 70",
        ]
        observations = [
            project_source_for_transport(
                registry,
                await registry.resolve(
                    PDFPageInput(document_id="OAK_2025_10K", document_sha256="b" * 64, page_number=number, text=text)
                ),
            )
            for number, text in enumerate(texts, 1)
        ]
        async with open_builtin_runtime(config, candidate_pipeline=Pipeline()) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(
                ScopeDraft(title="Reports", summary="Reports", idempotency_key="reports")
            )
            await runtime.ingestion.register(manifest_for_definition(PDF_PAGE_SOURCE_DEFINITION))
            for observation in observations:
                await runtime.ingestion.submit(
                    SubmitSourceObservation(scope_id=scope.scope_id, observation=observation)
                )
            memory = runtime.memory.for_scope(scope.scope_id)
            while (await memory.flush()).processed:
                pass
            citations = await search_pdf_pages(memory, "Is OAK a capital-intensive business in FY2025?", limit=20)
            actual = {
                (await memory.get(GetMemoryEntryRequest(citation=citation))).entry.sources[0].source_id
                for citation in citations
            }
            assert {observation.name for observation in observations[-3:]} <= actual
            assert len(citations) == len(actual) == 20

    asyncio.run(scenario())
