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
from dataclasses import dataclass
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
    frozenset({"litigation", "legal", "lawsuit", "lawsuits", "battles", "proceedings", "disputes", "claims"}),
    frozenset({"capex", "capital", "expenditures", "spending", "investment", "investments"}),
    frozenset({"ebitda", "operating", "income", "depreciation", "amortization"}),
    frozenset({"sales", "revenue", "revenues", "turnover"}),
    frozenset({"pnl", "earnings", "income", "profit", "loss", "margin"}),
    frozenset({"restructuring", "reorganization", "charges", "costs", "expenses"}),
    frozenset({"inventory", "inventories", "stock", "lifo", "fifo"}),
    frozenset({"receivables", "receivable", "accounts", "notes"}),
    frozenset({"debt", "borrowings", "loans", "notes", "credit", "liquidity"}),
    frozenset({"tax", "taxes", "taxation", "deferred"}),
    frozenset({"pension", "retirement", "postretirement", "benefit", "benefits"}),
    frozenset({"lease", "leases", "rent", "rental"}),
    frozenset({"acquisition", "acquisitions", "divestiture", "divestitures", "merger", "mergers"}),
    frozenset({"segment", "segments", "division", "divisions"}),
    frozenset({"assets", "liabilities", "equity", "capitalization"}),
    frozenset({"dividend", "dividends", "repurchase", "repurchases", "buyback", "buybacks"}),
    frozenset({"risk", "risks", "uncertainty", "uncertainties", "exposure", "exposures"}),
)


@dataclass(frozen=True, slots=True)
class _SectionRule:
    """A question trigger, independent Memory probes, and a page-title prior."""

    trigger: str
    queries: tuple[str, ...]
    heading: str
    boost: float = 12.0


@dataclass(frozen=True, slots=True)
class _SearchView:
    query: str
    weight: float


# A question can name several sections needed for one calculation. Probe each
# independently so lexical admission on the full question cannot hide a statement.
_REPORT_SECTIONS = (
    _SectionRule(
        r"income statements?|statements? of (?:income|operations|earnings)|profit and loss|p\s*&\s*l",
        ("statement income", "statements operations", "statement earnings"),
        r"(?:(?:consolidated|combined)\s+)?(?:statements? of (?:income|operations|earnings)|income statements?)",
    ),
    _SectionRule(
        r"cash[ -]flow statements?|statements? of cash flows?",
        ("statement cash flows", "statements cash flows"),
        r"(?:(?:consolidated|combined)\s+)?(?:statements? of cash flows?|cash flow statements?)",
    ),
    _SectionRule(
        r"balance sheets?|statements? of financial position",
        ("balance sheets", "statement financial position"),
        r"(?:(?:consolidated|combined)\s+)?(?:balance sheets?|statements? of financial position)",
    ),
    _SectionRule(
        r"legal (?:battles|proceedings|disputes)|litigation|lawsuits?",
        ("legal proceedings", "litigation contingencies"),
        r"(?:item\s+3\.?\s+)?(?:legal proceedings|litigation|legal contingencies)",
    ),
    _SectionRule(
        r"geograph\w*|countries|regions|primarily operates?|where .{0,20}\boperates?\b",
        ("geographic regions countries markets", "our divisions reportable segments"),
        r"(?:our )?(?:business|geograph\w*|divisions|reportable segments)",
        9.0,
    ),
    _SectionRule(
        r"segments?|divisions?",
        ("reportable segments divisions",),
        r"(?:our )?(?:divisions|reportable segments|segment information)",
        10.0,
    ),
    _SectionRule(
        r"restructuring|reorganization|productivity plan|severance",
        ("restructuring productivity severance",),
        r"(?:restructuring|productivity|severance)",
        8.0,
    ),
    _SectionRule(
        r"inventor(?:y|ies)|\blifo\b|\bfifo\b",
        ("inventory lifo fifo",),
        r"(?:inventor(?:y|ies)|inventory valuation)",
        7.0,
    ),
    _SectionRule(
        r"receivables?|accounts and notes receivable",
        ("accounts notes receivable",),
        r"(?:receivables?|accounts and notes receivable)",
        7.0,
    ),
    _SectionRule(
        r"property,? plant (?:and|&) equipment|ppe|fixed assets?",
        ("property plant equipment",),
        r"(?:property,? plant (?:and|&) equipment|fixed assets?)",
        8.0,
    ),
    _SectionRule(
        r"debt|borrowings?|liquidity|credit facilities?|long-term notes?",
        ("debt borrowings liquidity credit",),
        r"(?:debt|borrowings?|liquidity|credit facilities?)",
        7.0,
    ),
    _SectionRule(
        r"income taxes?|tax rate|effective tax",
        ("income taxes effective tax",),
        r"(?:income taxes?|taxes on income)",
        7.0,
    ),
    _SectionRule(
        r"pension|retirement benefits?|postretirement",
        ("pension retirement benefits",),
        r"(?:pension|retirement benefits?|postretirement)",
        7.0,
    ),
    _SectionRule(
        r"leases?|rent expense",
        ("leases rent",),
        r"(?:leases?|lease cost)",
        6.0,
    ),
    _SectionRule(
        r"acquisitions?|divestitures?|mergers?",
        ("acquisitions divestitures",),
        r"(?:acquisitions?|divestitures?|business combinations)",
        7.0,
    ),
    _SectionRule(
        r"shareholders.? equity|stockholders.? equity|dividends?|repurchases?|buybacks?",
        ("shareholders equity dividends repurchases",),
        r"(?:shareholders.? equity|stockholders.? equity|dividends|share repurchases?)",
        7.0,
    ),
)


def _requested_sections(question: str) -> tuple[_SectionRule, ...]:
    return tuple(
        section for section in _REPORT_SECTIONS if re.search(rf"\b(?:{section.trigger})\b", question, re.IGNORECASE)
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
    "rely",
    "relying",
    "define",
    "defined",
])


def _terms(text: str) -> list[str]:
    return re.findall(r"[a-z]+|\d+", text.lower())


def _is_year(term: str) -> bool:
    return re.fullmatch(r"(?:19|20)\d{2}", term) is not None


def _term_variants(term: str) -> tuple[str, ...]:
    """Return a small morphological expansion without a model or lexicon dependency."""
    if not term.isalpha() or len(term) < 4:
        return ()
    variants: set[str] = set()
    if term.endswith("ies") and len(term) > 5:
        variants.add(term[:-3] + "y")
    elif term.endswith("ing") and len(term) > 5:
        root = term[:-3]
        variants.update({root, root + "e", root + "ion", root + "ions"})
        if len(root) > 2 and root[-1] == root[-2]:
            variants.add(root[:-1])
    elif term.endswith("ed") and len(term) > 4:
        root = term[:-2]
        variants.update({root, root + "ing", root + "ion", root + "ions"})
        if len(root) > 2 and root[-1] == root[-2]:
            variants.add(root[:-1])
    elif term.endswith("es") and len(term) > 4:
        variants.add(term[:-2])
        variants.add(term[:-1])
    elif term.endswith("s") and not term.endswith("ss") and len(term) > 3:
        variants.add(term[:-1])
    else:
        variants.update({term + "s", term + "ed", term + "ing"})
    return tuple(sorted(variant for variant in variants if len(variant) >= 3))


def _merge_views(views: list[_SearchView]) -> tuple[_SearchView, ...]:
    by_query: dict[str, _SearchView] = {}
    for view in views:
        query = " ".join(view.query.split())
        if not query:
            continue
        previous = by_query.get(query)
        if previous is None or view.weight > previous.weight:
            by_query[query] = _SearchView(query=query, weight=view.weight)
    return tuple(by_query.values())


def _search_queries(question: str, sections: tuple[_SectionRule, ...]) -> tuple[list[str], tuple[_SearchView, ...]]:
    terms = list(dict.fromkeys(t for t in _terms(question) if t not in _STOP_WORDS))
    # Fiscal-year spelling is query syntax, not a different year in the report.
    terms = [t for t in terms if t != "fy"]
    if not terms:
        return [], ()
    groups = [group for group in _FINANCIAL_TERMS if group.intersection(terms)]
    ranked_terms = sorted(
        terms,
        key=lambda term: (
            any(term in group for group in groups),
            len(term),
            term,
        ),
        reverse=True,
    )
    focus_terms = [term for term in ranked_terms if not _is_year(term) and len(term) >= 4]
    high_signal = focus_terms[:8]
    views = [_SearchView(query=" ".join(terms), weight=1.6)]
    if high_signal and high_signal != terms:
        views.append(_SearchView(query=" ".join(high_signal), weight=1.1))
    views.extend(_SearchView(query=query, weight=1.0) for section in sections for query in section.queries)
    views.extend(_SearchView(query=" ".join(sorted(group)), weight=0.55) for group in groups[:3])
    views.extend(_SearchView(query=term, weight=0.25) for term in focus_terms[:2])
    return terms, _merge_views(views)


def _hit_key(hit: MemoryHit) -> tuple[str, str, int, str, str]:
    return (
        hit.memory_ref.family,
        hit.memory_ref.artifact_id,
        hit.memory_ref.revision,
        hit.entry_id,
        hit.entry_version_id,
    )


def _looks_like_table_of_contents(body: str) -> bool:
    lines = [line.strip() for line in body.splitlines() if line.strip()][:100]
    section_entries = sum(1 for line in lines if re.match(r"^(?:item|note)\s+\d+(?:[a-z])?\b", line, re.IGNORECASE))
    return section_entries >= 4


def _heading_boost(body: str, sections: tuple[_SectionRule, ...]) -> float:
    # Financial reports frequently place Item 3 after a property table. Scan
    # enough of the page to see a section start without treating body prose as a title.
    lines = [line.strip() for line in body.splitlines() if line.strip()][:64]
    matched = []
    for section in sections:
        heading = re.compile(
            rf"(?:{section.heading})(?:\s*[.:])?(?:\s*\(continued\))?(?:\s*\(in [^)]*\))?",
            re.IGNORECASE,
        )
        if any(heading.fullmatch(line) for line in lines):
            matched.append(section.boost)
    if not matched:
        return 0.0
    return max(matched) + 0.25 * (sum(matched) - max(matched))


def _ranking_weights(terms: list[str]) -> dict[str, float]:
    weights = {term: 0.3 if _is_year(term) else 1.0 for term in terms}
    for term in terms:
        for variant in _term_variants(term):
            weights.setdefault(variant, 0.35)
    for group in _FINANCIAL_TERMS:
        if group.intersection(terms):
            for term in group:
                weights.setdefault(term, 0.45)
    return weights


def _page_rank_score(
    hit: MemoryHit,
    count: Counter[str],
    terms: list[str],
    sections: tuple[_SectionRule, ...],
    agreement: dict[tuple[str, str, int, str, str], float],
    weights: dict[str, float],
    lengths: list[int],
    average_length: float,
    frequencies: Counter[str],
    hit_count: int,
    index: int,
) -> float:
    score = 0.0
    for term, weight in weights.items():
        frequency = count[term]
        if frequency:
            inverse = math.log(1 + (hit_count - frequencies[term] + 0.5) / (frequencies[term] + 0.5))
            score += (
                weight * inverse * frequency * 2.2 / (frequency + 1.2 * (0.25 + 0.75 * lengths[index] / average_length))
            )

    label, _, body = hit.text.partition("\n")
    query_terms = frozenset(terms)
    score += 3.0 * len(set(_terms(label)).intersection(query_terms))
    score += 6.0 * agreement.get(_hit_key(hit), 0.0)
    score += _heading_boost(body, sections)
    score += 0.8 * len(set(count).intersection(query_terms))
    title_window = set(_terms("\n".join(body.splitlines()[:12])))
    score += 0.4 * len(title_window.intersection(query_terms))
    if _looks_like_table_of_contents(body):
        score -= 8.0
    return score


def _rank_pages(
    hits: list[MemoryHit],
    terms: list[str],
    sections: tuple[_SectionRule, ...],
    agreement: dict[tuple[str, str, int, str, str], float],
) -> list[MemoryHit]:
    """BM25 plus section, term-shape, and multi-probe agreement over real Memory text."""
    counts = [Counter(_terms(hit.text)) for hit in hits]
    if not counts:
        return []
    lengths = [sum(count.values()) for count in counts]
    average = sum(lengths) / len(lengths) or 1
    frequencies = Counter(term for count in counts for term in count)
    weights = _ranking_weights(terms)
    scores = [
        _page_rank_score(
            hit,
            count,
            terms,
            sections,
            agreement,
            weights,
            lengths,
            average,
            frequencies,
            len(counts),
            index,
        )
        for index, (hit, count) in enumerate(zip(hits, counts, strict=True))
    ]
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
    terms, views = _search_queries(question, sections)
    hits: dict[tuple[str, str, int, str, str], MemoryHit] = {}
    agreement: dict[tuple[str, str, int, str, str], float] = {}
    candidate_limit = min(320, max(160, limit * 12))
    for view in views:
        result = await memory.search(SearchMemoryRequest(query=view.query, mode="fts", limit=candidate_limit))
        for rank, hit in enumerate(result.hits):
            key = _hit_key(hit)
            hits.setdefault(key, hit)
            agreement[key] = agreement.get(key, 0.0) + view.weight / (10 + rank)
    citations: list[MemoryCitation] = []
    seen: set[str] = set()
    for hit in _rank_pages(list(hits.values()), terms, sections, agreement):
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
