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

from pydantic import BaseModel, Field, field_validator, model_validator

from powercontext.sources._pdf_retrieval import search_pdf_pages
from powercontext.sources.definitions import AdapterSourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence


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

    from powercontext.sources._pdf_content import extract_page_text

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
                text=extract_page_text(page, reader),
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

__all__ = ["PDF_PAGE_SOURCE_DEFINITION", "PDFDocumentInput", "PDFPageInput", "parse_pdf_document", "search_pdf_pages"]
