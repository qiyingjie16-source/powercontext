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

"""Pi archive validation and lifecycle recovery behavior."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy

import pytest

from powercontext.sources import ConnectorBinding, SourceDefinitionRegistry, SourceRef, project_source_for_transport
from powercontext.sources.connectors import ConnectorLifecycle
from powercontext.sources.pi_session import (
    PI_EVENT_SOURCE_DEFINITION,
    PiSessionConnector,
    PiSessionInput,
    parse_pi_session,
)
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY


def archive(records, session_id="session"):
    return "\n".join(
        json.dumps(row, ensure_ascii=False)
        for row in [
            {"type": "session", "version": 3, "id": session_id},
            *records,
        ]
    ).encode()


def message(event_id, parent, text):
    return {"type": "message", "id": event_id, "parentId": parent, "message": {"role": "user", "content": text}}


@pytest.mark.parametrize(
    "record",
    [
        {"type": "message", "id": "a"},
        {"type": "model_change", "id": "a", "parentId": []},
        {"type": "model_change", "id": "a", "parentId": "later"},
        {"type": "model_change", "id": 1, "parentId": None},
        {"type": "model_change", "id": "", "parentId": None},
        {"type": "model_change", "id": "a", "parentId": "a"},
        [],
        None,
    ],
)
def test_invalid_unselected_nodes_reject_entire_archive(record):
    with pytest.raises(ValueError):
        parse_pi_session(PiSessionInput(content=archive([message("root", None, "ok"), record]), leaf_id="root"))


def test_all_text_blocks_and_sorted_unicode_tool_arguments_are_preserved():
    records = [
        message("root", None, "  original\r\ntext  "),
        {
            "type": "message",
            "id": "call",
            "parentId": "root",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "hidden"},
                    {"type": "text", "text": "before"},
                    {"type": "toolCall", "name": "read", "id": "c", "arguments": {"z": 1, "a": "中文"}},
                    {"type": "image", "data": "hidden"},
                    {"type": "text", "text": "after"},
                ],
            },
        },
        {
            "type": "compaction",
            "id": "summary",
            "parentId": "call",
            "summary": "summary",
            "firstKeptEntryId": "summary",
        },
    ]
    events = parse_pi_session(PiSessionInput(content=archive(records)))
    assert [event.content for event in events] == [
        "user:   original\r\ntext  ",
        'assistant: before\n[tool_call read c] {"a":"中文","z":1}\nafter',
        "compaction: summary",
    ]
    assert events[1].record == records[1]


def test_empty_archive_controls_multiple_roots_and_explicit_leaf():
    assert parse_pi_session(PiSessionInput(content=archive([]))) == ()
    records = [
        message("a", None, "first"),
        {"type": "label", "id": "control", "parentId": "a"},
        message("b", None, "second"),
    ]
    assert [v.event_id for v in parse_pi_session(PiSessionInput(content=archive(records)))] == ["b"]
    assert [v.event_id for v in parse_pi_session(PiSessionInput(content=archive(records), leaf_id="control"))] == ["a"]
    with pytest.raises(ValueError):
        parse_pi_session(PiSessionInput(content=archive([]), leaf_id="missing"))


def test_long_unicode_tool_result_and_identity_survive_source_serialization():
    text = "结果\n" * 20000
    record = {
        "type": "message",
        "id": "event:/" * 100,
        "parentId": None,
        "message": {
            "role": "toolResult",
            "toolName": "read",
            "toolCallId": "c",
            "content": [{"type": "text", "text": text}],
        },
    }
    (event,) = parse_pi_session(PiSessionInput(content=archive([record], "session:/" * 100)))

    async def scenario():
        registry = SourceDefinitionRegistry((PI_EVENT_SOURCE_DEFINITION,))
        source = await registry.resolve(event)
        assert len(source.name) <= 256
        restored = type(source).model_validate_json(source.model_dump_json())
        assert await registry.read(restored) == event
        evidence = project_source_for_transport(registry, restored).projection(TEXT_EVIDENCE_PROJECTION_KEY)
        assert evidence["content"] == "toolResult: [tool_result read c] " + text
        (other,) = parse_pi_session(PiSessionInput(content=archive([record], "other")))
        assert (await registry.resolve(other)).name != source.name

    asyncio.run(scenario())


class Progress:
    def __init__(self):
        self.value = None

    async def load(self, binding):
        return deepcopy(self.value)

    async def save(self, binding, checkpoint, *, expected):
        assert self.value == expected
        self.value = deepcopy(checkpoint)


class Sink:
    def __init__(self):
        self.saved = {}
        self.fail_on = None

    async def submit(self, binding, item_id, definition_name, value):
        if value.event_id == self.fail_on:
            raise OSError("injected persistence failure")  # noqa: TRY003
        if item_id in self.saved:
            assert self.saved[item_id] == value
        self.saved[item_id] = value
        return SourceRef(source_type=definition_name, source_id=item_id)


def test_partial_failure_replay_inactive_branch_rewrite_and_record_metadata(tmp_path):
    async def scenario():
        path = tmp_path / "session.jsonl"
        records = [message("a", None, "root"), message("left", "a", "left"), message("right", "a", "right")]
        path.write_bytes(archive(records))
        progress, sink = Progress(), Sink()
        binding = ConnectorBinding(
            scope_id="scope", binding_id="pi", connector_name="pi-session-history", connector_version="1"
        )

        async def run(leaf):
            return await ConnectorLifecycle(sink=sink, checkpoints=progress).run(
                PiSessionConnector(path, leaf), binding
            )

        sink.fail_on = "left"
        with pytest.raises(OSError):
            await run("left")
        assert progress.value is None and len(sink.saved) == 1
        sink.fail_on = None
        await run("left")
        assert len(sink.saved) == 2
        assert len((await run("right")).items) == 1
        prior = deepcopy(progress.value)
        records[1]["timestamp"] = "changed metadata without changing text"
        path.write_bytes(archive(records))
        with pytest.raises(ValueError, match="rewritten"):
            await run("right")
        assert progress.value == prior and len(sink.saved) == 3
        del records[1]["timestamp"]
        path.write_bytes(archive(records))
        assert (await run("right")).items == ()
        assert progress.value == prior
        # Serialization and a fresh store instance model a host restoring opaque progress.
        restarted = Progress()
        restarted.value = json.loads(json.dumps(progress.value))
        result = await ConnectorLifecycle(sink=sink, checkpoints=restarted).run(
            PiSessionConnector(path, "left"), binding
        )
        assert result.items == () and restarted.value == prior

    asyncio.run(scenario())
