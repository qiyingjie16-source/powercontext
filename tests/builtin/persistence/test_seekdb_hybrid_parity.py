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

"""Compare native hybrid and separate channel queries in the same seekdb."""

import asyncio
import math

import pytest

from powercontext.builtin.artifacts.memory import EmbeddingProfile, MemoryEntryInput, MemorySearchRequest
from powercontext.builtin.inference import EmbeddingResult
from powercontext.builtin.persistence.memory_index import CompositeMemoryIndex
from powercontext.builtin.persistence.seekdb import SeekDBConfig
from powercontext.builtin.persistence.seekdb.memory_index import SeekDBMemoryIndex
from powercontext.builtin.runtime import BuiltinConfig, open_builtin_contexts


def _vector(index: float) -> tuple[float, ...]:
    angle = index / 200
    return (math.cos(angle), math.sin(angle), *(0.0 for _ in range(382)))


class _Embedding:
    profile = EmbeddingProfile(
        profile_id="hybrid-parity", model="deterministic", dimension=384, distance="l2", normalization="unit"
    )

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        return EmbeddingResult(vectors=tuple(_vector(int(text.split()[0])) for text in texts))


def test_native_channels_match_separate_queries_on_a_bounded_candidate_pool(tmp_path) -> None:
    pytest.importorskip("pylibseekdb")

    async def scenario() -> None:
        embedding = _Embedding()
        config = BuiltinConfig(database=SeekDBConfig(path=tmp_path / "db"))
        async with open_builtin_contexts(config, embedding_model=embedding) as contexts:
            index = contexts.index
            assert isinstance(index, SeekDBMemoryIndex)
            baseline = CompositeMemoryIndex(*index.indexes)
            for scope in ("first", "second"):
                memory = (await contexts.get(scope)).artifacts.memory
                head = await memory.remember(
                    memory=None,
                    entries=tuple(
                        MemoryEntryInput(kind="fact", text=f"{i} report keyword {i % 3} in {scope}") for i in range(173)
                    ),
                    mode="append",
                )
                assert head is not None
                async with contexts.database.transaction() as connection:
                    for query in ("keyword report", "keyword 2"):
                        request = MemorySearchRequest(
                            query=query,
                            analyzed_query=query,
                            memories=(head.as_ref(),),
                            candidate_limit=40,
                            mode="hybrid",
                            query_vector=_vector(29.37),
                            embedding_profile=embedding.profile,
                        )
                        expected = await baseline.search(connection, scope, request)
                        actual = await index.search(connection, scope, request)
                        assert actual == expected

    asyncio.run(scenario())
