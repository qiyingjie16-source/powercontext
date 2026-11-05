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
from itertools import combinations
from typing import TYPE_CHECKING
from weakref import WeakKeyDictionary

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

    if b"%PDF-" not in value.content[:1024] or b"%%EOF" not in value.content[-16384:]:
        raise ValueError("Unable to completely parse PDF document")  # noqa: TRY003
    digest = hashlib.sha256(value.content).hexdigest()
    try:
        reader = PdfReader(io.BytesIO(value.content))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("PDF requires a non-empty password")  # noqa: TRY003, TRY301
        pages = tuple(
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
    if not pages:
        raise ValueError("PDF must contain at least one page")  # noqa: TRY003
    return pages


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

_ACRONYM_PATTERN = re.compile(r"\b(?:r&d|d&a|sg&a|sga|pp&e|p&l|cogs|eps)\b", re.IGNORECASE)
_ACRONYMS = {
    "r&d": "research development",
    "d&a": "depreciation amortization",
    "sg&a": "selling general administrative",
    "sga": "selling general administrative",
    "pp&e": "property plant equipment",
    "p&l": "income statement",
    "cogs": "cost goods sold",
    "eps": "earnings per share",
}

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


@dataclass(frozen=True, slots=True)
class _PageIndex:
    cache_key: tuple[str, str, int]
    hits: tuple[MemoryHit, ...]


# A question can name several sections needed for one calculation. Probe each
# independently so lexical admission on the full question cannot hide a statement.
_REPORT_SECTIONS = (
    _SectionRule(
        (
            r"income statements?|statements? of (?:income|operations|earnings)|profit and loss|p\s*&\s*l"
            r"|ebitda|gross (?:profit|margin)|operating (?:income|profit|margin)|net income"
        ),
        ("statement income", "statements operations", "statement earnings"),
        r"(?:(?:consolidated|combined)\s+)?(?:statements? of (?:income|operations|earnings)|income statements?)",
    ),
    _SectionRule(
        r"cash[ -]flow statements?|statements? of cash flows?|capex|capital expenditures?|depreciation|amortization",
        ("statement cash flows", "statements cash flows"),
        r"(?:(?:consolidated|combined)\s+)?(?:statements? of cash flows?|cash flow statements?)",
    ),
    _SectionRule(
        (
            r"balance sheets?|statements? of financial position|quick ratio|current ratio|working capital"
            r"|total (?:assets|liabilities)|inventory turnover|receivables? turnover"
        ),
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
    (r"quick ratio|acid[- ]test", ("cash short term investments", "accounts receivable", "current liabilities")),
    (r"current ratio", ("current assets", "current liabilities")),
    (r"inventory turnover|days inventory|inventory days", ("inventory", "cost sales", "cost revenue")),
    (
        r"receivable turnover|days sales outstanding|\bdso\b",
        ("accounts receivable", "net revenue", "sales"),
    ),
    (r"asset turnover", ("total assets", "net revenue", "sales")),
    (
        r"return on (?:assets|equity|invested capital)|\broa\b|\broe\b|\broic\b",
        ("net income", "total assets", "equity"),
    ),
    (r"interest coverage", ("interest expense", "operating income")),
    (r"payables?|days payable", ("accounts payable", "cost sales")),
    (r"employees?|headcount|workforce", ("employees workforce", "number employees")),
    (r"cyclical|seasonal", ("seasonal cyclical", "economic conditions")),
    (r"customer concentration|major customers?", ("largest customers", "percentage net revenue")),
    (r"product categor\w*|brands?", ("product categories", "brand portfolio")),
    (r"climate|environmental", ("climate environmental", "emissions sustainability")),
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
    "fiscal",
    "year",
    "years",
    "annual",
    "report",
    "reported",
    "reporting",
    "company",
    "corporation",
    "inc",
    "based",
    "according",
    "given",
    "show",
    "state",
    "then",
    "if",
    "not",
    "explicitly",
    "directly",
    "outlined",
    "quantity",
    "amount",
    "total",
    "approximately",
    "dollar",
    "dollars",
    "usd",
    "million",
    "millions",
    "billion",
    "billions",
    "percentage",
    "percent",
    "change",
    "primarily",
    "important",
    "ongoing",
    "materially",
])


def _terms(text: str) -> list[str]:
    return re.findall(r"[a-z]+|\d+", text.lower())


def _question_terms(question: str) -> list[str]:
    normalized = re.sub(r"\binvestment analyst\b", "", question.lower())
    normalized = _ACRONYM_PATTERN.sub(lambda match: _ACRONYMS[match.group(0).lower()], normalized)
    return list(dict.fromkeys(term for term in _terms(normalized) if term not in _STOP_WORDS and term != "fy"))


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


def _search_queries(
    question: str, sections: tuple[_SectionRule, ...]
) -> tuple[list[str], tuple[_SearchView, ...], list[str]]:
    terms = _question_terms(question)
    if not terms:
        return [], (), []
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
    concepts = [
        query
        for trigger, probes in _CONCEPT_PROBES
        if re.search(rf"\b(?:{trigger})\b", question, re.IGNORECASE)
        for query in probes
    ]
    concept_terms = list(
        dict.fromkeys(
            term for query in concepts for term in _terms(query) if term not in _STOP_WORDS and term not in terms
        )
    )
    views = [_SearchView(query=" ".join(terms), weight=1.6)]
    if high_signal and high_signal != terms:
        views.append(_SearchView(query=" ".join(high_signal), weight=1.1))
    views.extend(_SearchView(query=query, weight=1.0) for section in sections for query in section.queries)
    views.extend(_SearchView(query=query, weight=0.75) for query in concepts[:3])
    views.extend(_SearchView(query=" ".join(sorted(group)), weight=0.55) for group in groups[:3])
    views.extend(_SearchView(query=" ".join(pair), weight=0.4) for pair in combinations(focus_terms[:3], 2))
    views.extend(_SearchView(query=term, weight=0.25) for term in focus_terms[:2])
    return terms, _merge_views(views), concept_terms


def _hit_key(hit: MemoryHit) -> tuple[str, str, int, str, str]:
    return (
        hit.memory_ref.family,
        hit.memory_ref.artifact_id,
        hit.memory_ref.revision,
        hit.entry_id,
        hit.entry_version_id,
    )


def _document_of(hit: MemoryHit) -> str:
    return hit.text.partition("\n")[0].strip()


def _candidate_documents(hits: list[MemoryHit]) -> set[str]:
    documents = {_document_of(hit) for hit in hits}
    identified = {
        document
        for document in documents
        if "_" in document or re.search(r"(?:19|20)\d{2}|\b(?:10|8)[kq]\b", document, re.IGNORECASE)
    }
    return identified or documents


def _document_name_parts(document_id: str) -> list[str]:
    parts = re.findall(r"[a-z0-9]+", document_id.casefold())
    return [
        part
        for part in parts
        if part not in {"annual", "company", "corp", "corporation", "earnings", "group", "inc", "report", "k", "q"}
        and not _is_year(part)
        and not re.fullmatch(r"(?:10|8)[kq]", part)
    ]


def _document_aliases(document_id: str) -> tuple[str, ...]:
    meaningful = _document_name_parts(document_id)
    if not meaningful:
        return ()
    joined = "".join(meaningful)
    aliases = {joined}
    aliases.update(part for part in meaningful if len(part) >= 3)
    return tuple(sorted(aliases, key=lambda alias: (-len(alias), alias)))


def _target_document(question: str, hits: list[MemoryHit]) -> str | None:
    compact = re.sub(r"[^a-z0-9]", "", question.casefold())
    matched = []
    for document in sorted(_candidate_documents(hits)):
        aliases = _document_aliases(document)
        if any(len(alias) >= 2 and alias in compact for alias in aliases):
            matched.append((max((len(alias) for alias in aliases), default=0), document))
    if not matched:
        return None
    matched.sort(reverse=True)
    return matched[0][1] if len(matched) == 1 or matched[0][0] > matched[1][0] else None


def _dominant_document(hits: list[MemoryHit], agreement: dict[tuple[str, str, int, str, str], float]) -> str | None:
    mass: dict[str, float] = {}
    candidates = _candidate_documents(hits)
    for hit in hits:
        document = _document_of(hit)
        if document not in candidates:
            continue
        mass[document] = mass.get(document, 0.0) + agreement.get(_hit_key(hit), 0.0)
    ranked = sorted(mass.items(), key=lambda item: (-item[1], item[0]))
    if len(ranked) == 1:
        return ranked[0][0]
    return ranked[0][0] if len(ranked) > 1 and ranked[0][1] >= 1.75 * ranked[1][1] else None


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
    return 0.4 * (max(matched) + 0.25 * (sum(matched) - max(matched)))


def _ranking_weights(terms: list[str], concept_terms: list[str]) -> dict[str, float]:
    weights = {term: 0.3 if _is_year(term) else 1.0 for term in terms}
    for term in terms:
        for variant in _term_variants(term):
            weights.setdefault(variant, 0.35)
    for group in _FINANCIAL_TERMS:
        if group.intersection(terms):
            for term in sorted(group):
                weights.setdefault(term, 1.2)
    for term in concept_terms:
        weights.setdefault(term, 0.55)
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
    target_document: str | None,
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
    heading_boost = _heading_boost(body, sections)
    agreement_value = agreement.get(_hit_key(hit), 0.0)
    if score <= 0:
        return 0.0
    query_terms = frozenset(terms)
    label_matches = sorted(set(_terms(label)).intersection(query_terms))
    score += math.fsum(0.75 if term.isdigit() else 3.0 for term in label_matches)
    score += 0.5 * agreement_value
    score += heading_boost
    score += 0.8 * len(set(count).intersection(query_terms))
    title_window = set(_terms("\n".join(body.splitlines()[:12])))
    score += 0.4 * len(title_window.intersection(query_terms))
    if sections:
        numeric_density = sum(frequency for term, frequency in count.items() if term.isdigit()) / max(1, lengths[index])
        score += 2.5 * numeric_density
    if _looks_like_table_of_contents(body):
        score -= 8.0
    if score > 0 and target_document is not None and _document_of(hit) == target_document:
        score += 10.0
    return score


def _rank_pages(
    hits: list[MemoryHit],
    terms: list[str],
    concept_terms: list[str],
    sections: tuple[_SectionRule, ...],
    agreement: dict[tuple[str, str, int, str, str], float],
    target_document: str | None,
) -> list[MemoryHit]:
    """BM25 plus section, document, morphology, and multi-probe agreement."""
    counts = [Counter(_terms(hit.text.partition("\n")[2] or hit.text)) for hit in hits]
    if not counts:
        return []
    lengths = [sum(count.values()) for count in counts]
    average = sum(lengths) / len(lengths) or 1
    frequencies = Counter(term for count in counts for term in count)
    weights = _ranking_weights(terms, concept_terms)
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
            target_document,
        )
        for index, (hit, count) in enumerate(zip(hits, counts, strict=True))
    ]
    return [
        hits[index]
        for index in sorted(
            (position for position, value in enumerate(scores) if value > 0),
            key=lambda position: (
                -scores[position],
                hits[position].entry_id,
                hits[position].entry_version_id,
            ),
        )
    ]


_PAGE_INDEX_CACHE: WeakKeyDictionary[ScopedMemoryApplication, _PageIndex] = WeakKeyDictionary()


def _page_text_priority(text: str) -> tuple[bool, int, int]:
    label = text.partition("\n")[0].strip()
    identified = "_" in label or re.search(r"(?:19|20)\d{2}|\b(?:10|8)[kq]\b", label, re.IGNORECASE) is not None
    return identified, len(label), len(text)


async def _load_page_index(memory: ScopedMemoryApplication) -> _PageIndex:
    """Read the current scoped page set through the public Memory listing API."""
    from powercontext.builtin.artifacts.memory.models import MemoryHit

    listing = await memory.list()
    if listing.memory_ref is None:
        return _PageIndex(cache_key=(memory.scope_id, "", 0), hits=())
    cache_key = (memory.scope_id, listing.memory_ref.artifact_id, listing.memory_ref.revision)
    cached = _PAGE_INDEX_CACHE.get(memory)
    if cached is not None and cached.cache_key == cache_key:
        return cached

    by_page: dict[str, MemoryHit] = {}
    for record in listing.entries:
        if record.state != "active":
            continue
        page_ids = tuple(
            sorted(reference.source_id for reference in record.entry.sources if reference.source_type == "pdf-page")
        )
        if len(page_ids) != 1:
            continue
        hit = MemoryHit(
            memory_ref=record.memory_ref,
            entry_id=record.entry.entry_id,
            entry_version_id=record.entry.entry_version_id,
            text=record.entry.text,
            score=0.0,
            matched_by=(),
        )
        previous = by_page.get(page_ids[0])
        if previous is None or _page_text_priority(hit.text) > _page_text_priority(previous.text):
            by_page[page_ids[0]] = hit
    index = _PageIndex(cache_key=cache_key, hits=tuple(by_page.values()))
    _PAGE_INDEX_CACHE[memory] = index
    return index


async def _collect_views(
    memory: ScopedMemoryApplication,
    search_views: tuple[_SearchView, ...],
    candidate_limit: int,
    agreement: dict[tuple[str, str, int, str, str], float],
) -> None:
    from powercontext.builtin.runtime import SearchMemoryRequest

    for view in search_views:
        result = await memory.search(SearchMemoryRequest(query=view.query, mode="fts", limit=candidate_limit))
        for rank, hit in enumerate(result.hits):
            key = _hit_key(hit)
            agreement[key] = agreement.get(key, 0.0) + view.weight / (10 + rank)


async def search_pdf_pages(
    memory: ScopedMemoryApplication, question: str, limit: int = 10
) -> tuple[MemoryCitation, ...]:
    """Retrieve distinct page citations exclusively from the caller's scoped Memory."""
    from powercontext.builtin.artifacts.memory.models import MemoryCitation
    from powercontext.builtin.runtime import GetMemoryEntryRequest

    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("PDF search limit must be a non-negative integer")  # noqa: TRY003
    if limit == 0 or not question.strip():
        return ()
    sections = _requested_sections(question)
    terms, views, concept_terms = _search_queries(question, sections)
    if not terms:
        return ()
    page_index = await _load_page_index(memory)
    if not page_index.hits:
        return ()
    hit_list = list(page_index.hits)
    agreement: dict[tuple[str, str, int, str, str], float] = {}
    candidate_limit = min(320, max(160, limit * 12))

    await _collect_views(memory, views, candidate_limit, agreement)
    explicit_target = _target_document(question, hit_list)
    target_document = explicit_target or _dominant_document(hit_list, agreement)
    if explicit_target is not None:
        hit_list = [hit for hit in hit_list if _document_of(hit) == explicit_target]

    citations: list[MemoryCitation] = []
    seen: set[str] = set()
    for hit in _rank_pages(hit_list, terms, concept_terms, sections, agreement, target_document):
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
