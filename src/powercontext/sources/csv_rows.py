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

"""Offline CSV records with stable identities and captured text evidence."""

from __future__ import annotations

import csv
import io
import json
from urllib.parse import quote

from pydantic import BaseModel, field_validator, model_validator

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.definitions import AdapterSourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence


def _validate_document_id(value: str) -> str:
    if not value or value != value.strip():
        raise ValueError("document_id must be non-empty and have no surrounding whitespace")  # noqa: TRY003
    return value


def _validate_columns(columns: tuple[str, ...], primary_key: str) -> None:
    if not columns or any(not column.strip() for column in columns):
        raise ValueError("CSV headers must be non-empty and not blank")  # noqa: TRY003
    if len(set(columns)) != len(columns):
        raise ValueError("CSV headers must be unique")  # noqa: TRY003
    if not primary_key or primary_key not in columns:
        raise ValueError("primary_key must exactly match a CSV header")  # noqa: TRY003


def csv_row_source_id(document_id: str, primary_key_value: str) -> str:
    """Identify a record independently of its position or non-key values."""
    _validate_document_id(document_id)
    if not primary_key_value.strip():
        raise ValueError("primary_key_value must not be blank")  # noqa: TRY003
    identity = f"csv:{quote(document_id, safe='')}:{quote(primary_key_value, safe='')}"
    if len(identity) > MAX_SOURCE_ID_LENGTH:
        raise ValueError("CSV row source_id exceeds the source identity length limit")  # noqa: TRY003
    return identity


class CSVDocumentInput(BaseModel):
    """A complete UTF-8 CSV document and its exact primary-key column."""

    document_id: str
    primary_key: str
    content: bytes

    @field_validator("document_id")
    @classmethod
    def validate_document_id(cls, value: str) -> str:
        return _validate_document_id(value)

    @field_validator("primary_key")
    @classmethod
    def validate_primary_key(cls, value: str) -> str:
        if not value:
            raise ValueError("primary_key must not be empty")  # noqa: TRY003
        return value


class CSVRowInput(BaseModel):
    """One complete record, preserving column order and original strings."""

    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]

    @model_validator(mode="after")
    def validate_record(self) -> CSVRowInput:
        _validate_columns(self.columns, self.primary_key)
        if len(self.values) != len(self.columns):
            raise ValueError("CSV record width must match the header")  # noqa: TRY003
        if self.values[self.columns.index(self.primary_key)] != self.primary_key_value:
            raise ValueError("primary_key_value must match the record's primary-key field")  # noqa: TRY003
        csv_row_source_id(self.document_id, self.primary_key_value)
        return self


class CSVRowSource(Source):
    """A captured record that remains readable without the original CSV file."""

    materialization: SourceMaterialization = SourceMaterialization.CAPTURED
    source_id: str
    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]

    @model_validator(mode="after")
    def validate_record_identity(self) -> CSVRowSource:
        if self.materialization != SourceMaterialization.CAPTURED:
            raise ValueError("CSV row Sources must retain captured records")  # noqa: TRY003
        row = CSVRowInput.model_validate(self.model_dump(include=set(CSVRowInput.model_fields)))
        expected = csv_row_source_id(row.document_id, row.primary_key_value)
        if self.source_id != expected or self.name != expected:
            raise ValueError("CSV Source name and source_id must match its record identity")  # noqa: TRY003
        return self


def parse_csv_document(value: CSVDocumentInput) -> tuple[CSVRowInput, ...]:
    """Validate the entire document before returning any records."""
    text = value.content.decode("utf-8-sig", errors="strict")
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        columns = tuple(next(reader, ()))
        _validate_columns(columns, value.primary_key)
        key_index = columns.index(value.primary_key)
        rows: list[CSVRowInput] = []
        seen: set[str] = set()
        for fields in reader:
            if not fields:
                continue
            if len(fields) != len(columns):
                raise ValueError("CSV record width must match the header")  # noqa: TRY003
            key = fields[key_index]
            if key in seen:
                raise ValueError("CSV primary-key values must be unique within a document")  # noqa: TRY003
            rows.append(
                CSVRowInput(
                    document_id=value.document_id,
                    primary_key=value.primary_key,
                    primary_key_value=key,
                    columns=columns,
                    values=tuple(fields),
                )
            )
            seen.add(key)
    except csv.Error as error:
        raise ValueError(f"Malformed CSV: {error}") from error  # noqa: TRY003
    return tuple(rows)


class CSVRowAdapter:
    """Resolve and read self-contained CSV records."""

    name = "csv-row"
    input_class: type[CSVRowInput] = CSVRowInput
    source_class: type[CSVRowSource] = CSVRowSource

    async def resolve(self, value: CSVRowInput, /) -> CSVRowSource:
        identity = csv_row_source_id(value.document_id, value.primary_key_value)
        return CSVRowSource(name=identity, source_id=identity, **value.model_dump())

    async def read(self, source: CSVRowSource, /) -> CSVRowInput:
        return CSVRowInput.model_validate(source.model_dump(include=set(CSVRowInput.model_fields)))


class CSVRowTextEvidenceProjection:
    """Expose every original field as ordered, compact JSON text."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class: type[CSVRowSource] = CSVRowSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: CSVRowSource, /) -> TextEvidence:
        row = dict(zip(source.columns, source.values, strict=True))
        return TextEvidence(
            source_type="csv-row",
            source_id=source.source_id,
            content=json.dumps(row, ensure_ascii=False, separators=(",", ":")),
            metadata={
                "document_id": source.document_id,
                "primary_key": source.primary_key,
                "primary_key_value": source.primary_key_value,
                "columns": list(source.columns),
                "row": row,
            },
        )


CSV_ROW_SOURCE_DEFINITION: AdapterSourceDefinition[CSVRowInput, CSVRowSource, CSVRowInput] = AdapterSourceDefinition(
    CSVRowAdapter(),
    projections=(CSVRowTextEvidenceProjection(),),
)

__all__ = [
    "CSV_ROW_SOURCE_DEFINITION",
    "CSVDocumentInput",
    "CSVRowInput",
    "CSVRowSource",
    "csv_row_source_id",
    "parse_csv_document",
]
