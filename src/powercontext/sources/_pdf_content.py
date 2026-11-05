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

"""Validate page text programs before pypdf can recover partial evidence.

Stream validation adapted from Nvole/OceanBase-PowerContext (Apache-2.0).
Only page content and invoked Forms are decoded; image data is not text evidence.
"""

from __future__ import annotations

import zlib
from copy import copy
from typing import TYPE_CHECKING, cast

from pypdf.filters import decode_stream_data
from pypdf.generic import ArrayObject, ContentStream, NameObject, NullObject, StreamObject

if TYPE_CHECKING:
    from pypdf import PageObject, PdfReader
    from pypdf.generic import DictionaryObject, PdfObject


def extract_page_text(page: PageObject, reader: PdfReader) -> str:
    _validate_content(page.get("/Contents"), page.get("/Resources", {}), reader, set(), set())
    return page.extract_text() or ""


def _validate_content(
    value: PdfObject | None,
    resources: DictionaryObject,
    reader: PdfReader,
    active: set[int],
    visited: set[tuple[int, int]],
) -> None:
    if value is None:
        return
    content = value.get_object()
    if isinstance(content, NullObject):
        return
    if content is None:
        raise ValueError("PDF content refers to a missing stream")  # noqa: TRY003
    if hasattr(resources, "get_object"):
        resources = cast("DictionaryObject", resources.get_object())
    identity = id(content)
    key = (identity, id(resources))
    if identity in active:
        raise ValueError("PDF content contains a cyclic Form reference")  # noqa: TRY003
    if key in visited:
        return
    active.add(identity)
    try:
        _validate_filters(content)
        _validate_forms(ContentStream(content, reader), resources, reader, active, visited)
    finally:
        active.remove(identity)
    visited.add(key)


def _validate_forms(
    program: ContentStream,
    resources: DictionaryObject,
    reader: PdfReader,
    active: set[int],
    visited: set[tuple[int, int]],
) -> None:
    xobjects = resources.get("/XObject", {})
    if hasattr(xobjects, "get_object"):
        xobjects = xobjects.get_object()
    for operands, operator in program.operations:
        if operator != b"Do":
            continue
        if not operands or operands[0] not in xobjects:
            raise ValueError("PDF page refers to a missing XObject")  # noqa: TRY003
        form = xobjects[operands[0]].get_object()
        if form.get("/Subtype") == "/Form":
            _validate_content(form, form.get("/Resources", resources), reader, active, visited)


def _validate_filters(content: PdfObject) -> None:
    if isinstance(content, ArrayObject):
        for part in content:
            _validate_filters(part.get_object())
        return
    if isinstance(content, NullObject):
        return
    if not isinstance(content, StreamObject):
        raise TypeError("PDF content must contain streams")  # noqa: TRY003
    filters = content.get("/Filter", ())
    if hasattr(filters, "get_object"):
        filters = filters.get_object()
    if isinstance(filters, NullObject):
        return
    if not isinstance(filters, (ArrayObject, tuple)):
        filters = (filters,)
    for index, name in enumerate(filters):
        preceding = copy(content)
        preceding[NameObject("/Filter")] = ArrayObject(filters[:index])
        data = decode_stream_data(preceding)
        _validate_encoded_data(name, data)


def _validate_encoded_data(name: str, data: bytes) -> None:
    if name in {"/FlateDecode", "/Fl"}:
        decoder = zlib.decompressobj()
        decoder.decompress(data, 65536)
        while decoder.unconsumed_tail and not decoder.eof:
            decoder.decompress(decoder.unconsumed_tail, 65536)
        complete = decoder.eof
    elif name in {"/ASCII85Decode", "/A85"}:
        complete = data.rstrip(b"\x00 \t\n\r\f").endswith(b"~>")
    elif name in {"/ASCIIHexDecode", "/AHx"}:
        complete = data.rstrip(b"\x00 \t\n\r\f").endswith(b">")
    elif name in {"/RunLengthDecode", "/RL"}:
        complete = _run_length_complete(data)
    else:
        return
    if not complete:
        raise ValueError("PDF content has an incomplete encoded stream")  # noqa: TRY003


def _run_length_complete(data: bytes) -> bool:
    offset = 0
    while offset < len(data):
        length = data[offset]
        if length == 128:
            return True
        offset += 1 + (length + 1 if length < 128 else 1)
    return False
