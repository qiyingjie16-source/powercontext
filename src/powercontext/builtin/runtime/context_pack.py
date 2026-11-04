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

"""Canonical full and prefix-delta transport for prepared context bytes."""
# ruff: noqa: TRY003

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass

_PACK_SCHEMA = "powercontext.context-pack.v1"
_DELTA_SCHEMA = "powercontext.context-pack-delta.v1"


class ContextPackError(ValueError):
    """Invalid UTF-8 chunk or context-pack wire payload."""


def _wire(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _encoded(items: tuple[bytes, ...]) -> list[str]:
    return [base64.b64encode(item).decode("ascii") for item in items]


def _items(value: object) -> tuple[bytes, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ContextPackError("items must be Base64 strings")
    try:
        result = tuple(base64.b64decode(item, validate=True) for item in value)
    except (ValueError, binascii.Error) as error:
        raise ContextPackError("invalid Base64 chunk") from error
    if _encoded(result) != value:
        raise ContextPackError("noncanonical Base64 chunk")
    _validate_items(result)
    return result


def _validate_items(items: tuple[bytes, ...]) -> None:
    if not isinstance(items, tuple):
        raise ContextPackError("items must be a bytes tuple")
    for item in items:
        if not isinstance(item, bytes):
            raise ContextPackError("items must contain only bytes")
        try:
            item.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ContextPackError("each item must be valid UTF-8") from error


def _digest(items: tuple[bytes, ...]) -> str:
    return "sha256:" + hashlib.sha256(_wire({"schema": _PACK_SCHEMA, "items": _encoded(items)})).hexdigest()


def _valid_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 71
        and value.startswith("sha256:")
        and all(c in "0123456789abcdef" for c in value[7:])
    )


def _unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ContextPackError("duplicate JSON field")
        result[key] = value
    return result


def _decode(raw: bytes) -> object:
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_fields,
            parse_constant=lambda _: (_ for _ in ()).throw(ContextPackError("invalid JSON constant")),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as error:
        raise ContextPackError("invalid context-pack JSON") from error


@dataclass(frozen=True, slots=True)
class ContextPack:
    items: tuple[bytes, ...]
    digest: str
    byte_count: int

    @classmethod
    def build(cls, items: tuple[bytes, ...]) -> ContextPack:
        _validate_items(items)
        digest = _digest(items)
        encoded = _encoded(items)
        base = {"schema": _PACK_SCHEMA, "items": encoded, "digest": digest}
        count = 0
        while True:
            actual = len(_wire(base | {"byte_count": count}))
            if actual == count:
                return cls(items=items, digest=digest, byte_count=count)
            count = actual

    @classmethod
    def from_content(cls, content: str, *, chunk_bytes: int = 512) -> ContextPack:
        if isinstance(chunk_bytes, bool) or not isinstance(chunk_bytes, int) or chunk_bytes <= 0:
            raise ContextPackError("chunk_bytes must be a positive integer")
        if not isinstance(content, str):
            raise ContextPackError("content must be a string")
        items: list[bytes] = []
        part = bytearray()
        for character in content:
            try:
                encoded = character.encode("utf-8")
            except UnicodeEncodeError as error:
                raise ContextPackError("content must be valid UTF-8") from error
            if len(encoded) > chunk_bytes:
                raise ContextPackError("chunk_bytes cannot hold one UTF-8 character")
            if len(part) + len(encoded) > chunk_bytes:
                items.append(bytes(part))
                part.clear()
            part.extend(encoded)
        if part:
            items.append(bytes(part))
        return cls.build(tuple(items))

    def to_bytes(self) -> bytes:
        _validate_items(self.items)
        if self.digest != _digest(self.items):
            raise ContextPackError("pack digest mismatch")
        payload = _wire({
            "schema": _PACK_SCHEMA,
            "items": _encoded(self.items),
            "digest": self.digest,
            "byte_count": self.byte_count,
        })
        if type(self.byte_count) is not int or self.byte_count != len(payload):
            raise ContextPackError("pack byte_count mismatch")
        return payload

    @classmethod
    def from_bytes(cls, raw: bytes) -> ContextPack:
        data = _decode(raw)
        if (
            not isinstance(data, dict)
            or set(data) != {"schema", "items", "digest", "byte_count"}
            or data["schema"] != _PACK_SCHEMA
        ):
            raise ContextPackError("invalid pack fields or schema")
        items = _items(data["items"])
        result = cls.build(items)
        if (
            data["digest"] != result.digest
            or type(data["byte_count"]) is not int
            or data["byte_count"] != result.byte_count
        ):
            raise ContextPackError("pack digest or byte_count mismatch")
        return result


@dataclass(frozen=True, slots=True)
class ContextPackDelta:
    base_digest: str
    target_digest: str
    prefix_items: int
    appended: tuple[bytes, ...]

    @property
    def byte_count(self) -> int:
        return len(self.to_bytes())

    @classmethod
    def between(cls, base: ContextPack, target: ContextPack) -> ContextPackDelta:
        base.to_bytes()
        target.to_bytes()
        prefix = 0
        for left, right in zip(base.items, target.items, strict=False):
            if left != right:
                break
            prefix += 1
        return cls(base.digest, target.digest, prefix, target.items[prefix:])

    def to_bytes(self) -> bytes:
        if not _valid_digest(self.base_digest) or not _valid_digest(self.target_digest):
            raise ContextPackError("invalid delta digest")
        if type(self.prefix_items) is not int or self.prefix_items < 0:
            raise ContextPackError("invalid prefix_items")
        _validate_items(self.appended)
        return _wire({
            "schema": _DELTA_SCHEMA,
            "base_digest": self.base_digest,
            "target_digest": self.target_digest,
            "prefix_items": self.prefix_items,
            "appended": _encoded(self.appended),
        })

    @classmethod
    def from_bytes(cls, raw: bytes) -> ContextPackDelta:
        data = _decode(raw)
        if (
            not isinstance(data, dict)
            or set(data) != {"schema", "base_digest", "target_digest", "prefix_items", "appended"}
            or data["schema"] != _DELTA_SCHEMA
        ):
            raise ContextPackError("invalid delta fields or schema")
        result = cls(data["base_digest"], data["target_digest"], data["prefix_items"], _items(data["appended"]))
        result.to_bytes()
        return result


def apply_context_pack_delta(base: ContextPack, delta: ContextPackDelta) -> ContextPack:
    """Rebuild a target using only the supplied base pack and delta."""
    base.to_bytes()
    delta.to_bytes()
    if delta.base_digest != base.digest:
        raise ContextPackError("delta base digest mismatch")
    if delta.prefix_items > len(base.items):
        raise ContextPackError("delta prefix exceeds base items")
    target = ContextPack.build(base.items[: delta.prefix_items] + delta.appended)
    if target.digest != delta.target_digest:
        raise ContextPackError("delta target digest mismatch")
    return target


__all__ = ["ContextPack", "ContextPackDelta", "ContextPackError", "apply_context_pack_delta"]
