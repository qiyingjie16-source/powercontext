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

"""Durable source dependency availability with replayable native propagation."""
# ruff: noqa: TRY003

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator
from sqlalchemy import insert, select, update

from powercontext.artifacts import ArtifactRef, MemoryCitation
from powercontext.builtin.persistence.tables import SOURCE_LIFECYCLE_NODES_TABLE
from powercontext.builtin.runtime.models import GetMemoryEntryRequest
from powercontext.builtin.sources import validate_scope_id
from powercontext.sources import SourceRef

if TYPE_CHECKING:
    from powercontext.builtin.persistence.database import AsyncDatabase
    from powercontext.builtin.runtime.application import BuiltinRuntime


class SourceLifecycleError(ValueError):
    """Invalid graph binding, lifecycle event, or requested use."""


class SourceLifecycleEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    node_id: str
    revision: int = Field(gt=0)
    sequence: int = Field(gt=0)
    available: StrictBool
    reason: str

    @field_validator("node_id", "reason")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must be nonblank")
        return value


class _Node(BaseModel):
    node_id: str
    kind: Literal["source", "memory", "topic"]
    reference: SourceRef | MemoryCitation | ArtifactRef
    parents: tuple[str, ...] = ()
    policy: Literal["all", "any"] = "all"
    revision: int = 1
    available: bool = True
    event: SourceLifecycleEvent | None = None


class SourceLifecycleApplication:
    def __init__(self, runtime: BuiltinRuntime, database: AsyncDatabase) -> None:
        self._runtime = runtime
        self._database = database

    def for_scope(self, scope_id: str) -> ScopedSourceLifecycle:
        return ScopedSourceLifecycle(self._runtime, self._database, validate_scope_id(scope_id))


class ScopedSourceLifecycle:
    def __init__(self, runtime: BuiltinRuntime, database: AsyncDatabase, scope_id: str) -> None:
        self._runtime = runtime
        self._database = database
        self.scope_id = scope_id

    async def register_source(self, node_id: str, source_ref: SourceRef, *, revision: int) -> None:
        if type(revision) is not int or revision <= 0 or not isinstance(source_ref, SourceRef):
            raise SourceLifecycleError("invalid source registration")
        await self._register(_Node(node_id=node_id, kind="source", reference=source_ref, revision=revision))

    async def register_memory(
        self, node_id: str, citation: MemoryCitation, *, parents: tuple[str, ...], policy: str
    ) -> None:
        if not isinstance(citation, MemoryCitation):
            raise SourceLifecycleError("invalid Memory citation")
        await self._derived(node_id, "memory", citation, parents, policy)

    async def register_topic(
        self, node_id: str, artifact_ref: ArtifactRef, *, parents: tuple[str, ...], policy: str
    ) -> None:
        if not isinstance(artifact_ref, ArtifactRef) or artifact_ref.family != "topic-memory":
            raise SourceLifecycleError("invalid Topic reference")
        await self._derived(node_id, "topic", artifact_ref, parents, policy)

    async def _derived(self, node_id, kind, reference, parents, policy) -> None:
        if policy not in ("all", "any") or not isinstance(parents, tuple) or not parents:
            raise SourceLifecycleError("invalid dependency policy or parents")
        if any(not isinstance(parent, str) or not parent.strip() for parent in parents):
            raise SourceLifecycleError("invalid parent identity")
        if len(set(parents)) != len(parents):
            raise SourceLifecycleError("duplicate parents")
        await self._register(_Node(node_id=node_id, kind=kind, reference=reference, parents=parents, policy=policy))

    async def _load(self) -> dict[str, _Node]:
        table = SOURCE_LIFECYCLE_NODES_TABLE
        async with self._database.transaction() as connection:
            rows = await connection.scalars(select(table.c.payload).where(table.c.scope_id == self.scope_id))
            nodes = [_Node.model_validate_json(bytes(row)) for row in rows]
        return {node.node_id: node for node in nodes}

    async def _save(self, nodes: dict[str, _Node], *, new: str | None = None) -> None:
        table = SOURCE_LIFECYCLE_NODES_TABLE
        async with self._database.transaction() as connection:
            for node in nodes.values():
                values = {
                    "kind": node.kind,
                    "artifact_id": node.reference.artifact_id if isinstance(node.reference, ArtifactRef) else None,
                    "available": node.available,
                    "payload": node.model_dump_json().encode("utf-8"),
                }
                if node.node_id == new:
                    await connection.execute(insert(table).values(scope_id=self.scope_id, node_id=new, **values))
                else:
                    await connection.execute(
                        update(table)
                        .where(
                            table.c.scope_id == self.scope_id,
                            table.c.node_id == node.node_id,
                        )
                        .values(**values)
                    )

    async def _register(self, node: _Node) -> None:
        if not node.node_id.strip() or len(node.node_id) > 256:
            raise SourceLifecycleError("invalid node identity")
        async with self._runtime._scoped_operation(self.scope_id), self._runtime._locked(self.scope_id):
            nodes = await self._load()
            previous = nodes.get(node.node_id)
            if previous is not None:
                if previous.model_copy(update={"available": True, "event": None}) != node:
                    raise SourceLifecycleError("node binding conflict")
                await self._sync(nodes)
                return
            if node.node_id in node.parents or any(parent not in nodes for parent in node.parents):
                raise SourceLifecycleError("parents must already be registered and cannot include self")
            # Parent-first registration makes cycles impossible without graph mutation.
            try:
                if isinstance(node.reference, SourceRef):
                    await self._runtime.records.for_scope(self.scope_id).get_source(
                        node.reference.source_type,
                        node.reference.source_id,
                    )
                elif isinstance(node.reference, MemoryCitation):
                    await self._runtime.memory.for_scope(self.scope_id).get(
                        GetMemoryEntryRequest(citation=node.reference)
                    )
                else:
                    await self._runtime.records.for_scope(self.scope_id).get_artifact_revision(
                        node.reference.family,
                        node.reference.artifact_id,
                        node.reference.revision,
                    )
            except Exception as error:
                raise SourceLifecycleError("registration reference is not readable") from error
            nodes[node.node_id] = node
            _propagate(nodes)
            await self._save(nodes, new=node.node_id)
            await self._sync(nodes)

    async def apply(self, event: SourceLifecycleEvent) -> list[str]:
        async with self._runtime._scoped_operation(self.scope_id), self._runtime._locked(self.scope_id):
            nodes = await self._load()
            node = nodes.get(event.node_id)
            if node is None or node.kind != "source" or node.revision != event.revision:
                raise SourceLifecycleError("unknown Source or lifecycle revision mismatch")
            if node.event is not None and (
                event.sequence < node.event.sequence or (event.sequence == node.event.sequence and event != node.event)
            ):
                raise SourceLifecycleError("stale or conflicting event sequence")
            node.available = event.available
            node.event = event
            _propagate(nodes)
            # Commit desired state before native writes: replay repairs an interrupted propagation.
            await self._save(nodes)
            await self._sync(nodes)
            return sorted(node.node_id for node in nodes.values() if node.available)

    async def _sync(self, nodes: dict[str, _Node]) -> None:
        async with self._runtime._context(self.scope_id) as context:
            service = context.artifacts.memory
            for node in nodes.values():
                if not isinstance(node.reference, MemoryCitation):
                    continue
                current = await service.head(context.artifacts.memory_artifact_id)
                citation = node.reference
                entry = next(
                    (entry for entry in await service.entries(current) if entry.entry_id == citation.entry_id), None
                )
                if entry is None or entry.entry_version_id != citation.entry_version_id:
                    raise SourceLifecycleError("registered Memory body changed")
                state = next(item.state for item in current.content.manifest.entries if item.entry_id == entry.entry_id)
                target = "active" if node.available else "inactive"
                if state != target:
                    operation = service.reactivate if node.available else service.forget
                    await operation(current, entries=(entry,), reason="source lifecycle propagation")

    async def resolve(self, node_id: str, *, purpose: str):
        if purpose not in ("generation", "audit"):
            raise SourceLifecycleError("unknown lifecycle purpose")
        async with self._runtime._scoped_operation(self.scope_id):
            node = (await self._load()).get(node_id)
            if node is None or (purpose == "generation" and not node.available):
                raise SourceLifecycleError("unknown or unavailable node")
            return node.reference


def _propagate(nodes: dict[str, _Node]) -> None:
    visited: set[str] = set()

    def visit(node: _Node) -> None:
        if node.node_id in visited:
            return
        for parent in node.parents:
            visit(nodes[parent])
        if node.parents:
            support = (nodes[parent].available for parent in node.parents)
            node.available = all(support) if node.policy == "all" else any(support)
        visited.add(node.node_id)

    for node in nodes.values():
        visit(node)


__all__ = ["SourceLifecycleError", "SourceLifecycleEvent"]
