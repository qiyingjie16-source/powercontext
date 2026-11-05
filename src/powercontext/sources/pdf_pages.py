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

"""Whole-document PDF capture and offline page retrieval through scoped Memory."""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
from collections import Counter
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field, field_validator, model_validator

from powercontext.sources.definitions import AdapterSourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

if TYPE_CHECKING:
    from powercontext.builtin.artifacts.memory.models import MemoryCitation, MemoryHit
    from powercontext.builtin.runtime.application import ScopedMemoryApplication


class PDFDocumentInput(BaseModel):
    """The complete original PDF, with a caller-owned document identifier."""

    document_id: str
    content: bytes

    @field_validator("document_id")
    @classmethod
    def validate_document_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("PDF document_id must not be blank")  # noqa: TRY003
        return value


class PDFPageInput(BaseModel):
    """Extractable text from one physical page of an exact PDF revision."""

    document_id: str
    document_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_number: int = Field(ge=1, strict=True)
    text: str

    @field_validator("document_id")
    @classmethod
    def validate_document_id(cls, value: str) -> str:
        return PDFDocumentInput.validate_document_id(value)


def parse_pdf_document(value: PDFDocumentInput) -> tuple[PDFPageInput, ...]:
    """Extract every physical page before returning any captured input."""
    from pypdf import PdfReader

    digest = hashlib.sha256(value.content).hexdigest()
    try:
        reader = PdfReader(io.BytesIO(value.content), strict=True)
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("PDF requires a non-empty password")  # noqa: TRY003, TRY301
        return tuple(
            PDFPageInput(
                document_id=value.document_id,
                document_sha256=digest,
                page_number=number,
                text=page.extract_text() or "",
            )
            for number, page in enumerate(reader.pages, start=1)
        )
    except Exception as error:
        # Normalize parser/codec failures at the document boundary; never return partial pages.
        raise ValueError("Unable to completely parse PDF document") from error  # noqa: TRY003


def _identity(page: PDFPageInput) -> str:
    identity = json.dumps(
        [page.document_id, page.document_sha256, page.page_number], ensure_ascii=False, separators=(",", ":")
    )
    return "pdf:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


class PDFPageSource(Source):
    """An immutable-revision physical page, independent of the original file."""

    page: PDFPageInput
    materialization: SourceMaterialization = SourceMaterialization.CAPTURED

    @model_validator(mode="after")
    def validate_page_identity(self) -> PDFPageSource:
        if self.materialization != SourceMaterialization.CAPTURED or self.name != _identity(self.page):
            raise ValueError("PDF Source must capture its exact page identity")  # noqa: TRY003
        return self


class PDFPageAdapter:
    name = "pdf-page"
    input_class: type[PDFPageInput] = PDFPageInput
    source_class: type[PDFPageSource] = PDFPageSource

    async def resolve(self, value: PDFPageInput, /) -> PDFPageSource:
        return PDFPageSource(name=_identity(value), page=value.model_copy(deep=True))

    async def read(self, source: PDFPageSource, /) -> PDFPageInput:
        return source.page.model_copy(deep=True)


class PDFTextEvidenceProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class: type[PDFPageSource] = PDFPageSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PDFPageSource, /) -> TextEvidence:
        page = source.page
        return TextEvidence(
            source_type="pdf-page",
            source_id=source.name,
            content=f"{page.document_id}\n{page.text}",
            metadata=page.model_dump(exclude={"text"}, mode="json"),
        )


PDF_PAGE_SOURCE_DEFINITION: AdapterSourceDefinition[PDFPageInput, PDFPageSource, PDFPageInput] = (
    AdapterSourceDefinition(PDFPageAdapter(), projections=(PDFTextEvidenceProjection(),))
)

# General financial vocabulary, independent of any report, question set, or evidence labels.
_FINANCIAL_TERMS = (
    frozenset({"geographies", "geography", "geographic", "geographical", "regions", "countries", "international"}),
    frozenset({"litigation", "legal", "lawsuit", "lawsuits", "battles", "proceedings", "contingencies"}),
    frozenset({"capex", "capital", "expenditures", "spending"}),
    frozenset({"ebitda", "operating", "income", "depreciation", "amortization"}),
    frozenset({"sales", "revenue", "revenues", "turnover"}),
    frozenset({"pnl", "earnings", "income", "profit", "loss"}),
    frozenset({"restructuring", "reorganization", "charges", "costs"}),
)
# A question can name several sections needed for one calculation. Probe each
# independently so lexical admission on the full question cannot hide a statement.
_REPORT_SECTIONS = (
    (
        r"income statements?|statements? of (?:income|operations|earnings)|profit and loss|p\s*&\s*l",
        ("statement income", "statements operations", "statement earnings"),
        r"(?:(?:consolidated|combined)\s+)?(?:statements? of (?:income|operations|earnings)|income statements?)",
    ),
    (
        r"cash[ -]flow statements?|statements? of cash flows?",
        ("statement cash flows", "statements cash flows"),
        r"(?:(?:consolidated|combined)\s+)?(?:statements? of cash flows?|cash flow statements?)",
    ),
    (
        r"balance sheets?|statements? of financial position",
        ("balance sheets", "statement financial position"),
        r"(?:(?:consolidated|combined)\s+)?(?:balance sheets?|statements? of financial position)",
    ),
    (
        r"legal (?:battles|proceedings|disputes)|litigation|lawsuits?",
        ("legal proceedings", "litigation contingencies"),
        r"(?:item\s+3\.?\s+)?(?:legal proceedings|litigation|legal contingencies)",
    ),
)


def _requested_sections(question: str) -> tuple[tuple[tuple[str, ...], str], ...]:
    return tuple(
        (queries, heading)
        for trigger, queries, heading in _REPORT_SECTIONS
        if re.search(rf"\b(?:{trigger})\b", question, re.IGNORECASE)
    )


# Short probes cover independently reported concepts in a compound question.
# The alternatives describe standard accounting terminology.
_CONCEPT_PROBES = (
    (r"capital expenditures?|capex", ("capital expenditures", "property equipment")),
    (r"depreciation|amortization|ebitda|d\s*&\s*a", ("depreciation amortization",)),
    (r"operating (?:income|profit|margin)|ebitda", ("operating income", "operating profit")),
    (r"free cash flow|operating cash flow|cash from operations", ("operating activities", "cash provided")),
    (r"restructur\w*|reorgani[sz]\w*", ("restructuring charges", "restructuring costs")),
    (r"geograph\w*|countr\w*|regions?", ("geographic segments", "business segments", "international operations")),
    (r"gross (?:profit|margin)|cost of (?:sales|revenue|goods)", ("gross profit", "cost sales", "cost revenue")),
    (r"dividends?|share repurchases?|buybacks?", ("dividends paid", "share repurchases")),
    (r"work(?:ing)? capital|current (?:assets|liabilities)|liquidity", ("current assets", "current liabilities")),
    (r"debt|borrowings|leverage", ("long term debt", "short term borrowings")),
)


_STOP_WORDS = frozenset([
    "a",
    "an",
    "the",
    "and",
    "or",
    "of",
    "for",
    "to",
    "in",
    "on",
    "at",
    "by",
    "from",
    "with",
    "as",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "being",
    "what",
    "which",
    "who",
    "how",
    "why",
    "when",
    "where",
    "do",
    "does",
    "did",
    "can",
    "could",
    "should",
    "would",
    "i",
    "we",
    "you",
    "it",
    "its",
    "this",
    "that",
    "these",
    "those",
    "any",
    "has",
    "have",
    "had",
    "give",
    "response",
    "answer",
    "respond",
    "question",
    "assuming",
    "assume",
    "perspective",
    "analyst",
    "details",
    "shown",
    "using",
    "use",
    "only",
    "calculate",
])


def _terms(text: str) -> list[str]:
    return re.findall(r"[a-z]+|\d+", text.lower())


def _search_queries(question: str, section_queries: tuple[str, ...]) -> tuple[list[str], tuple[str, ...]]:
    terms = list(dict.fromkeys(t for t in _terms(question) if t not in _STOP_WORDS))
    # Fiscal-year spelling is query syntax, not a different year in the report.
    terms = [t for t in terms if t != "fy"]
    if not terms:
        return [], ()
    expansions = [" ".join(sorted(group)) for group in _FINANCIAL_TERMS if group.intersection(terms)]
    concepts = [
        query
        for trigger, probes in _CONCEPT_PROBES
        if re.search(rf"\b(?:{trigger})\b", question, re.IGNORECASE)
        for query in probes
    ]
    queries = [" ".join(terms), *section_queries, *concepts, *expansions]
    return terms, tuple(dict.fromkeys(queries))


def _rank_pages(hits: list[MemoryHit], terms: list[str], headings: tuple[str, ...]) -> list[MemoryHit]:
    """BM25 over Memory-returned text, retaining original exact hit identities."""
    counts = [Counter(_terms(hit.text)) for hit in hits]
    if not counts:
        return []
    lengths = [sum(count.values()) for count in counts]
    average = sum(lengths) / len(lengths) or 1
    frequencies = Counter(term for count in counts for term in count)
    weights = dict.fromkeys(terms, 1.0)
    for group in _FINANCIAL_TERMS:
        if group.intersection(terms):
            for term in group:
                weights.setdefault(term, 0.4)
    scores = []
    for index, count in enumerate(counts):
        score = 0.0
        for term, weight in weights.items():
            frequency = count[term]
            if frequency:
                inverse = math.log(1 + (len(counts) - frequencies[term] + 0.5) / (frequencies[term] + 0.5))
                score += (
                    weight * inverse * frequency * 2.2 / (frequency + 1.2 * (0.25 + 0.75 * lengths[index] / average))
                )
        # Document labels carry company/year scope as well as searchable body text.
        label, _, body = hits[index].text.partition("\n")
        label_terms = set(_terms(label))
        # A section title is stronger evidence of the requested source than a
        # passing mention of that section in notes, certifications or a TOC.
        opening = "\n".join(body.splitlines()[:6])
        if any(re.search(rf"^\s*(?:{heading})[ \t]*[.:]?[ \t]*$", opening, re.I | re.M) for heading in headings):
            score += 12
        # Company tokens in the document ID distinguish identically headed
        # statements from different issuers. Year matches alone are weaker.
        score += math.fsum(0.75 if term.isdigit() else 3.0 for term in label_terms.intersection(terms))
        scores.append(score)
    return [hits[index] for index in sorted(range(len(hits)), key=lambda i: (-scores[i], i))]


async def search_pdf_pages(
    memory: ScopedMemoryApplication, question: str, limit: int = 10
) -> tuple[MemoryCitation, ...]:
    """Retrieve distinct page citations exclusively from the caller's scoped Memory."""
    from powercontext.builtin.artifacts.memory.models import MemoryCitation
    from powercontext.builtin.runtime import GetMemoryEntryRequest, SearchMemoryRequest

    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("PDF search limit must be a non-negative integer")  # noqa: TRY003
    if limit == 0 or not question.strip():
        return ()
    sections = _requested_sections(question)
    terms, queries = _search_queries(question, tuple(query for queries, _ in sections for query in queries))
    headings = tuple(heading for _, heading in sections)
    hits: dict[tuple[str, str, int, str, str], MemoryHit] = {}
    for query in queries:
        result = await memory.search(SearchMemoryRequest(query=query, mode="fts", limit=max(100, limit * 5)))
        for hit in result.hits:
            key = (
                hit.memory_ref.family,
                hit.memory_ref.artifact_id,
                hit.memory_ref.revision,
                hit.entry_id,
                hit.entry_version_id,
            )
            hits.setdefault(key, hit)
    citations: list[MemoryCitation] = []
    seen: set[str] = set()
    for hit in _rank_pages(list(hits.values()), terms, headings):
        citation = MemoryCitation(
            memory_ref=hit.memory_ref, entry_id=hit.entry_id, entry_version_id=hit.entry_version_id
        )
        entry = (await memory.get(GetMemoryEntryRequest(citation=citation))).entry
        pages = {source.source_id for source in entry.sources if source.source_type == "pdf-page"}
        if len(pages) != 1 or pages.intersection(seen):
            continue
        seen.update(pages)
        citations.append(citation)
        if len(citations) == limit:
            break
    return tuple(citations)


__all__ = ["PDF_PAGE_SOURCE_DEFINITION", "PDFDocumentInput", "PDFPageInput", "parse_pdf_document", "search_pdf_pages"]
