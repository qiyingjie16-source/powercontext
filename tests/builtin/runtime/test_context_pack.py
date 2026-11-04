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

"""Context-pack wire validation and measured prefix reuse."""

from __future__ import annotations

import base64
import json

import pytest

from powercontext.builtin.runtime.context_pack import (
    ContextPack,
    ContextPackDelta,
    ContextPackError,
    apply_context_pack_delta,
)


def wire(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def test_unicode_boundaries_every_chunk_reconstruct_exact_bytes():
    text = "海洋 🌊 alpha\n" * 100
    for size in (4, 5, 7, 32, 512):
        pack = ContextPack.from_content(text, chunk_bytes=size)
        assert all(len(part) <= size and part.decode("utf-8") for part in pack.items)
        assert b"".join(pack.items) == text.encode()
        assert ContextPack.from_bytes(pack.to_bytes()) == pack


@pytest.mark.parametrize("content,size", [("海", 2), ("abc", 0), ("abc", -1), ("abc", True)])
def test_impossible_utf8_or_invalid_bound_fails(content, size):
    with pytest.raises(ContextPackError):
        ContextPack.from_content(content, chunk_bytes=size)


def test_pack_wire_rejects_bad_base64_digest_count_and_extra_fields():
    pack = ContextPack.build((b"one", b"two"))
    payload = json.loads(pack.to_bytes())
    for update in (
        {"items": ["@@"]},
        {"items": [base64.b64encode(b"changed").decode()]},
        {"byte_count": True},
        {"byte_count": pack.byte_count + 1},
        {"extra": 1},
    ):
        with pytest.raises(ContextPackError):
            ContextPack.from_bytes(wire(payload | update))
    with pytest.raises(ContextPackError):
        ContextPack.build((b"\xff",))


def test_delta_wire_rejects_bad_prefix_extra_fields_and_wrong_target():
    base = ContextPack.build((b"one", b"two"))
    target = ContextPack.build((b"one", b"THREE"))
    delta = ContextPackDelta.between(base, target)
    payload = json.loads(delta.to_bytes())
    for update in ({"prefix_items": True}, {"prefix_items": 1.5}, {"appended": ["@@"]}, {"extra": 1}):
        with pytest.raises(ContextPackError):
            ContextPackDelta.from_bytes(wire(payload | update))
    with pytest.raises(ContextPackError):
        apply_context_pack_delta(base, ContextPackDelta(delta.base_digest, delta.target_digest, 3, delta.appended))
    with pytest.raises(ContextPackError):
        apply_context_pack_delta(base, ContextPackDelta(delta.base_digest, "sha256:" + "0" * 64, 1, delta.appended))


def test_40_round_wire_savings_include_first_pack_and_all_headers():
    base_text = "meeting participant evidence and decision context\n" * 100
    previous = None
    raw_bytes = 0
    transmitted = 0
    for turn in range(40):
        # Several distinct contexts with retained prefixes and later suffix changes.
        text = base_text + ("follow-up decision and action item\n" * (turn // 10 + 1))
        target = ContextPack.from_content(text)
        raw_bytes += len(text.encode())
        if previous is None:
            transmitted += len(target.to_bytes())
        else:
            delta = ContextPackDelta.between(previous, target)
            transmitted += len(delta.to_bytes())
            assert (
                apply_context_pack_delta(
                    ContextPack.from_bytes(previous.to_bytes()), ContextPackDelta.from_bytes(delta.to_bytes())
                )
                == target
            )
        previous = target
    assert 1 - transmitted / raw_bytes >= 0.45


def test_duplicate_wire_fields_fail():
    pack = ContextPack.build((b"evidence",))
    raw = pack.to_bytes()
    with pytest.raises(ContextPackError):
        ContextPack.from_bytes(b'{"schema":"powercontext.context-pack.v1",' + raw[1:])
