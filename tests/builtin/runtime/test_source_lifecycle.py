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

"""Lifecycle durability and interrupted native state propagation."""

import asyncio

import pytest
from pydantic import ValidationError

from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.persistence.memory import RelationalMemoryBackend
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import BuiltinConfig, CaptureSource, RememberMemoryRequest, open_builtin_runtime
from powercontext.builtin.runtime.source_lifecycle import SourceLifecycleError, SourceLifecycleEvent
from powercontext.builtin.scope import ScopeDraft
from powercontext.sources import SourceRef


def test_event_fields_are_strict():
    valid = {"node_id": "root", "revision": 1, "sequence": 1, "available": False, "reason": "withdrawn"}
    for changes in (
        {"revision": True},
        {"revision": 1.0},
        {"sequence": "1"},
        {"sequence": 0},
        {"available": 0},
        {"reason": "  "},
        {"node_id": ""},
        {"extra": "x"},
    ):
        with pytest.raises(ValidationError):
            SourceLifecycleEvent(**(valid | changes))


def test_partial_native_commit_failure_retries_after_restart(tmp_path, monkeypatch):
    async def run():
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'retry.db'}"))
        event = SourceLifecycleEvent(node_id="root", revision=1, sequence=1, available=False, reason="withdraw")
        refs = []
        async with open_builtin_runtime(config) as runtime:
            scope = (
                await runtime.scopes.create(ScopeDraft(title="retry", summary="retry", idempotency_key="retry"))
            ).scope_id
            lifecycle = runtime.lifecycle.for_scope(scope)
            source = await runtime.sources.for_scope(scope).capture(
                CaptureSource(source_id="root", content="evidence", metadata={})
            )
            await lifecycle.register_source("root", source.source_ref, revision=1)
            for index in range(2):
                result = await runtime.memory.for_scope(scope).remember(
                    RememberMemoryRequest(entries=(MemoryEntryInput(kind="fact", text=f"fact {index}"),))
                )
                refs.append(result.entry.citation)
                await lifecycle.register_memory(f"m{index}", refs[-1], parents=("root",), policy="all")
            original = RelationalMemoryBackend._commit
            attempts = 0

            async def fail_second(backend, connection, commit):
                nonlocal attempts
                attempts += 1
                if attempts == 2:
                    raise OSError("controlled persistence failure")  # noqa: TRY003
                return await original(backend, connection, commit)

            with monkeypatch.context() as patch:
                patch.setattr(RelationalMemoryBackend, "_commit", fail_second)
                with pytest.raises(OSError):
                    await lifecycle.apply(event)
            page = await runtime.memory.for_scope(scope).list(include_inactive=True)
            assert sorted(entry.state for entry in page.entries) == ["active", "inactive"]
        async with open_builtin_runtime(config) as runtime:
            lifecycle = runtime.lifecycle.for_scope(scope)
            assert await lifecycle.apply(event) == []
            page = await runtime.memory.for_scope(scope).list(include_inactive=True)
            assert all(entry.state == "inactive" for entry in page.entries)
            await lifecycle.apply(event)
            assert (await runtime.memory.for_scope(scope).list(include_inactive=True)).memory_ref == page.memory_ref
            for index, ref in enumerate(refs):
                assert await lifecycle.resolve(f"m{index}", purpose="audit") == ref
                with pytest.raises(SourceLifecycleError):
                    await lifecycle.resolve(f"m{index}", purpose="generation")
            await lifecycle.apply(event.model_copy(update={"sequence": 2, "available": True}))
            for index, ref in enumerate(refs):
                assert await lifecycle.resolve(f"m{index}", purpose="generation") == ref

    asyncio.run(run())


def test_scope_isolation_and_missing_reference():
    async def run():
        async with open_builtin_runtime(BuiltinConfig(database=SQLiteConfig())) as runtime:
            services = []
            for title in ("first", "second"):
                scope = (
                    await runtime.scopes.create(ScopeDraft(title=title, summary=title, idempotency_key=title))
                ).scope_id
                lifecycle = runtime.lifecycle.for_scope(scope)
                source = await runtime.sources.for_scope(scope).capture(
                    CaptureSource(source_id="root", content=title, metadata={})
                )
                await lifecycle.register_source("root", source.source_ref, revision=1)
                services.append(lifecycle)
            await services[0].apply(
                SourceLifecycleEvent(node_id="root", revision=1, sequence=1, available=False, reason="withdraw")
            )
            assert await services[1].resolve("root", purpose="generation") == SourceRef(
                source_type="content", source_id="root"
            )
            with pytest.raises(SourceLifecycleError):
                await services[1].register_source(
                    "missing", SourceRef(source_type="content", source_id="absent"), revision=1
                )

    asyncio.run(run())
