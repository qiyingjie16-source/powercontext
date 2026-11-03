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

"""Capture raw visible events from a Pi v3 session tree."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import cast

from pydantic import BaseModel, Field, JsonValue, model_validator

from powercontext.sources.connectors import ConnectorRunCompletion, ConnectorRunSession, ConnectorRunStatus
from powercontext.sources.definitions import AdapterSourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence


class PiSessionInput(BaseModel):
    """An offline Pi archive and optional selected tree node."""

    content: bytes
    leaf_id: str | None = None


class PiEventInput(BaseModel):
    """An original record and its complete visible text."""

    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _identity(session_id: str, event_id: str) -> str:
    if not session_id or not event_id:
        raise ValueError("Pi session and event IDs must not be empty")  # noqa: TRY003
    return "pi:" + hashlib.sha256(_json([session_id, event_id]).encode("utf-8")).hexdigest()


def _nonfinite(value: str) -> None:
    raise ValueError(f"Invalid JSON constant: {value}")  # noqa: TRY003


def _archive(value: PiSessionInput) -> tuple[str, dict[str, dict[str, JsonValue]], str | None]:
    records = [
        json.loads(line, parse_constant=_nonfinite)
        for line in value.content.decode("utf-8-sig").split("\n")
        if line.strip()
    ]
    if not records or not isinstance(records[0], dict):
        raise ValueError("Pi archive must begin with a session header")  # noqa: TRY003
    header = records[0]
    session_id = header.get("id")
    if (
        header.get("type") != "session"
        or type(header.get("version")) is not int
        or header["version"] != 3
        or not isinstance(session_id, str)
        or not session_id
    ):
        raise ValueError("Pi archive requires a version 3 session header with an ID")  # noqa: TRY003
    nodes: dict[str, dict[str, JsonValue]] = {}
    for record in records[1:]:
        if not isinstance(record, dict):
            raise ValueError("Pi entries must be JSON objects")  # noqa: TRY003, TRY004
        event_id = record.get("id")
        if not isinstance(event_id, str) or not event_id or event_id in nodes:
            raise ValueError("Pi entry IDs must be non-empty and unique")  # noqa: TRY003
        if "parentId" not in record:
            raise ValueError("Pi entry is missing parentId")  # noqa: TRY003
        parent = record["parentId"]
        if parent is not None and (not isinstance(parent, str) or parent not in nodes):
            raise ValueError("Pi parent must be null or identify an earlier entry")  # noqa: TRY003
        if not isinstance(record.get("type"), str) or not record["type"] or record["type"] == "session":
            raise ValueError("Pi entry must have a non-session type")  # noqa: TRY003
        nodes[event_id] = record
    leaf = value.leaf_id if value.leaf_id is not None else next(reversed(nodes), None)
    if leaf is not None and leaf not in nodes:
        raise ValueError("Selected Pi leaf does not exist")  # noqa: TRY003
    return session_id, nodes, leaf


def _text(value: JsonValue, *, tools: bool = False) -> str:
    if isinstance(value, str):
        return value
    parts: list[str] = []
    if isinstance(value, list):
        for block in value:
            if not isinstance(block, dict):
                continue
            text = block.get("text")
            if block.get("type") == "text" and isinstance(text, str):
                parts.append(text)
            elif tools and block.get("type") == "toolCall":
                parts.append(
                    f"[tool_call {block.get('name', '')} {block.get('id', '')}] {_json(block.get('arguments', {}))}"
                )
    return "\n".join(parts)


def _event(session_id: str, record: dict[str, JsonValue]) -> PiEventInput | None:
    kind = cast(str, record["type"])
    role, body = "", ""
    if kind in {"compaction", "branch_summary"}:
        role = str(kind)
        summary = record.get("summary")
        body = summary if isinstance(summary, str) else ""
    elif kind == "custom_message":
        role, body = "custom", _text(record.get("content"))
    elif kind == "message":
        message = record.get("message")
        if not isinstance(message, dict):
            raise ValueError("Pi message must be an object")  # noqa: TRY003
        raw_role = message.get("role")
        if not isinstance(raw_role, str) or not raw_role:
            raise ValueError("Pi message requires a role")  # noqa: TRY003
        role = raw_role
        body = _text(message.get("content"), tools=True)
        if role == "toolResult" and body:
            body = f"[tool_result {message.get('toolName', '')} {message.get('toolCallId', '')}] {body}"
        elif role == "bashExecution":
            command, output = message.get("command", ""), message.get("output", "")
            if not isinstance(command, str) or not isinstance(output, str):
                raise ValueError("Pi bash command and output must be strings")  # noqa: TRY003
            body = f"$ {command}\n{output}" if command or output else ""
    if not body:
        return None
    return PiEventInput(
        session_id=session_id,
        event_id=cast(str, record["id"]),
        parent_id=cast(str | None, record["parentId"]),
        event_type=kind,
        role=role,
        content=f"{role}: {body}",
        record=record,
    )


def _selected(session_id: str, nodes: dict[str, dict[str, JsonValue]], leaf: str | None) -> tuple[PiEventInput, ...]:
    chain: list[dict[str, JsonValue]] = []
    while leaf is not None:
        record = nodes[leaf]
        chain.append(record)
        leaf = cast(str | None, record["parentId"])
    return tuple(event for record in reversed(chain) if (event := _event(session_id, record)) is not None)


def parse_pi_session(value: PiSessionInput) -> tuple[PiEventInput, ...]:
    """Return the raw visible ancestor chain, without applying context compaction."""
    return _selected(*_archive(value))


class PiEventSource(Source):
    event: PiEventInput
    materialization: SourceMaterialization = SourceMaterialization.CAPTURED

    @model_validator(mode="after")
    def validate_event(self) -> PiEventSource:
        if self.materialization != SourceMaterialization.CAPTURED or self.name != _identity(
            self.event.session_id, self.event.event_id
        ):
            raise ValueError("Pi Source must capture its exact event identity")  # noqa: TRY003
        if _event(self.event.session_id, self.event.record) != self.event:
            raise ValueError("Pi event fields must match the original record")  # noqa: TRY003
        return self


class PiEventAdapter:
    name = "pi-event"
    input_class: type[PiEventInput] = PiEventInput
    source_class: type[PiEventSource] = PiEventSource

    async def resolve(self, value: PiEventInput, /) -> PiEventSource:
        return PiEventSource(name=_identity(value.session_id, value.event_id), event=value.model_copy(deep=True))

    async def read(self, source: PiEventSource, /) -> PiEventInput:
        return source.event.model_copy(deep=True)


class PiTextEvidenceProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class: type[PiEventSource] = PiEventSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PiEventSource, /) -> TextEvidence:
        event = source.event
        return TextEvidence(
            source_type="pi-event",
            source_id=source.name,
            content=event.content,
            metadata=event.model_dump(
                include={"session_id", "event_id", "parent_id", "event_type", "role"}, mode="json"
            ),
        )


PI_EVENT_SOURCE_DEFINITION: AdapterSourceDefinition[PiEventInput, PiEventSource, PiEventInput] = (
    AdapterSourceDefinition(PiEventAdapter(), projections=(PiTextEvidenceProjection(),))
)


class _Checkpoint(BaseModel):
    version: int = 1
    imported: dict[str, dict[str, str]] = Field(default_factory=dict)


class PiSessionConnector:
    """Import only new branch events; lifecycle owns durable progress commits."""

    name = "pi-session-history"
    version = "1"
    source_definitions = frozenset({"pi-event"})

    def __init__(self, path: str | Path, leaf_id: str | None = None) -> None:
        self.path = Path(path)
        self.leaf_id = leaf_id

    async def run(self, session: ConnectorRunSession, /) -> ConnectorRunCompletion:
        content = await asyncio.to_thread(self.path.read_bytes)
        session_id, nodes, leaf = _archive(PiSessionInput(content=content, leaf_id=self.leaf_id))
        events = _selected(session_id, nodes, leaf)
        checkpoint = _Checkpoint.model_validate(session.checkpoint) if session.checkpoint is not None else _Checkpoint()
        if checkpoint.version != 1:
            raise ValueError("Unsupported Pi checkpoint version")  # noqa: TRY003
        # Validate imported records even on inactive branches before any writes.
        digests = {key: hashlib.sha256(_json(record).encode("utf-8")).hexdigest() for key, record in nodes.items()}
        previous = checkpoint.imported.get(session_id, {})
        for event_id, digest in previous.items():
            if digests.get(event_id) != digest:
                raise ValueError("Previously imported Pi event was rewritten or removed")  # noqa: TRY003
        imported = dict(previous)
        for event in events:
            identity = _identity(session_id, event.event_id)
            if event.event_id not in imported:
                await session.submit(identity, "pi-event", event)
                imported[event.event_id] = digests[event.event_id]
        return ConnectorRunCompletion(
            status=ConnectorRunStatus.COMPLETE,
            checkpoint=_Checkpoint(imported=checkpoint.imported | {session_id: imported}).model_dump(mode="json"),
        )


__all__ = ["PI_EVENT_SOURCE_DEFINITION", "PiEventInput", "PiSessionConnector", "PiSessionInput", "parse_pi_session"]
