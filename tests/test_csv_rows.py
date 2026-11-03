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

"""Contract boundaries for offline CSV record capture."""

from __future__ import annotations

import asyncio
import csv
import io
import json

import pytest

from powercontext.sources import SourceDefinitionRegistry, SourceMaterialization, project_source_for_transport
from powercontext.sources.csv_rows import (
    CSV_ROW_SOURCE_DEFINITION,
    CSVDocumentInput,
    CSVRowInput,
    CSVRowSource,
    csv_row_source_id,
    parse_csv_document,
)
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY


def parse(content: bytes) -> tuple[CSVRowInput, ...]:
    return parse_csv_document(CSVDocumentInput(document_id="表:格", primary_key="id", content=content))


@pytest.mark.parametrize(
    "content",
    [
        b"\nid,name\n1,one\n",  # The first record, not the first nonblank record, is the header.
        b"id,name\n1,one,extra\n",
        b"id,name\n1,valid\n2\n",
        b"id,name\n1,valid\n \t,blank-key\n",
        b'id,name\n1,valid\n2,"bad"tail\n',
        b"id,name\n1,valid\n2,\xff\n",
        b"id,name\n1,one\n1,two\n",
        b"id,name\n,,\n",
    ],
)
def test_invalid_later_records_reject_the_whole_document(content: bytes) -> None:
    with pytest.raises(ValueError):
        parse(content)


def test_only_completely_empty_records_are_ignored() -> None:
    assert parse(b"id,name\r\n\r\n001,\r\n\r\n")[0].values == ("001", "")
    with pytest.raises(ValueError):
        parse(b"id,name\n \n")
    with pytest.raises(ValueError):
        parse(b'id\n""\n')


def test_unicode_and_csv_dialect_round_trip_through_transport() -> None:
    columns = ("id", "备注", " name ")
    values = (" 键:/% ", '中文,引号"\r\n第二行', "0001")
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(columns)
    writer.writerow(values)
    (row,) = parse(output.getvalue().encode("utf-8"))
    registry = SourceDefinitionRegistry((CSV_ROW_SOURCE_DEFINITION,))
    source = asyncio.run(registry.resolve(row))
    assert source.materialization == SourceMaterialization.CAPTURED
    restored = CSVRowSource.model_validate_json(source.model_dump_json())
    assert asyncio.run(registry.read(restored)) == row
    observation = project_source_for_transport(registry, restored)
    projection = observation.projection(TEXT_EVIDENCE_PROJECTION_KEY)
    assert projection["content"] == json.dumps(
        dict(zip(columns, values, strict=True)), ensure_ascii=False, separators=(",", ":")
    )
    assert projection["metadata"]["columns"] == list(columns)
    assert row.values == values


def test_identity_limit_applies_after_percent_encoding() -> None:
    assert len(csv_row_source_id("d", "x" * 250)) == 256
    with pytest.raises(ValueError):
        csv_row_source_id("d", "x" * 251)
    assert len(csv_row_source_id("d", "中" * 27)) == 249
    with pytest.raises(ValueError):
        csv_row_source_id("d", "中" * 28)
    assert csv_row_source_id("a:b", "c") != csv_row_source_id("a", "b:c")
    assert csv_row_source_id("a", "%2F") != csv_row_source_id("a", "/")


@pytest.mark.parametrize(
    "changes",
    [
        {"columns": ("id", "id")},
        {"columns": ("id", " ")},
        {"values": ("1",)},
        {"primary_key": " id "},
        {"primary_key_value": "2"},
        {"document_id": " doc"},
    ],
)
def test_direct_row_inputs_cannot_bypass_record_validation(changes: dict) -> None:
    payload = {
        "document_id": "doc",
        "primary_key": "id",
        "primary_key_value": "1",
        "columns": ("id", "name"),
        "values": ("1", "one"),
    }
    with pytest.raises(ValueError):
        CSVRowInput(**(payload | changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"name": "other"},
        {"source_id": "other"},
        {"materialization": "referenced"},
        {"primary_key_value": "other"},
    ],
)
def test_serialized_sources_reject_conflicting_identity(changes: dict) -> None:
    (row,) = parse(b"id,name\n1,one\n")
    registry = SourceDefinitionRegistry((CSV_ROW_SOURCE_DEFINITION,))
    source = asyncio.run(registry.resolve(row))
    with pytest.raises(ValueError):
        CSVRowSource.model_validate(source.model_dump() | changes)
