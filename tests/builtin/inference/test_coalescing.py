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

"""Shared in-flight work, partial failures, and batch-level usage."""

import asyncio

import pytest

from powercontext.builtin.artifacts.memory import EmbeddingProfile
from powercontext.builtin.inference import EmbeddingResult, InferenceUsage
from powercontext.builtin.inference.coalescing import CoalescingEmbeddingModel


class ControlledEmbedding:
    profile = EmbeddingProfile(profile_id="controlled", model="offline", dimension=1)

    def __init__(self):
        self.calls = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def embed(self, texts):
        self.calls.append(texts)
        self.started.set()
        await self.release.wait()
        if "bad" in texts:
            raise RuntimeError("failed batch")  # noqa: TRY003 - controlled provider failure
        return EmbeddingResult(
            vectors=tuple((float(len(text)),) for text in texts),
            usage=InferenceUsage(requests=3, input_tokens=7, output_tokens=2),
        )


def test_late_waiter_survives_cancellation_without_cache():
    async def run():
        base = ControlledEmbedding()
        model = CoalescingEmbeddingModel(base, coalesce_ms=0, cache_size=0)
        first = asyncio.create_task(model.embed(("alpha",)))
        await base.started.wait()
        late = asyncio.create_task(model.embed(("alpha", "alpha")))
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        base.release.set()
        result = await asyncio.wait_for(late, 2)
        assert result.vectors == ((5.0,), (5.0,))
        assert result.usage == InferenceUsage(requests=3, input_tokens=7, output_tokens=2)
        assert base.calls == [("alpha",)]
        await model.embed(("alpha",))
        assert base.calls == [("alpha",), ("alpha",)]

    asyncio.run(run())


def test_batch_failure_isolated_and_only_success_cached():
    async def run():
        base = ControlledEmbedding()
        model = CoalescingEmbeddingModel(base, max_batch_size=1, coalesce_ms=0)
        failed = asyncio.create_task(model.embed(("bad",)))
        healthy = asyncio.create_task(model.embed(("ok",)))
        await base.started.wait()
        base.release.set()
        with pytest.raises(RuntimeError, match="failed batch"):
            await failed
        assert (await healthy).vectors == ((2.0,),)
        assert (await model.embed(("ok",))).usage.requests == 0
        with pytest.raises(RuntimeError):
            await model.embed(("bad",))
        assert base.calls.count(("bad",)) == 2
        assert base.calls.count(("ok",)) == 1

    asyncio.run(run())


def test_usage_counts_unique_batches_for_each_caller():
    async def run():
        base = ControlledEmbedding()
        base.release.set()
        model = CoalescingEmbeddingModel(base, max_batch_size=2, coalesce_ms=0)
        first, second = await asyncio.gather(model.embed(("a", "bb", "ccc", "a")), model.embed(("bb", "ccc", "dddd")))
        assert first.vectors == ((1.0,), (2.0,), (3.0,), (1.0,))
        assert second.vectors == ((2.0,), (3.0,), (4.0,))
        for result in (first, second):
            assert result.usage == InferenceUsage(requests=6, input_tokens=14, output_tokens=4)
        assert (await model.embed(("a", "dddd"))).usage.requests == 0

    asyncio.run(run())


def test_all_waiters_cancelled_still_populates_cache():
    async def run():
        base = ControlledEmbedding()
        model = CoalescingEmbeddingModel(base, coalesce_ms=0)
        task = asyncio.create_task(model.embed(("retained",)))
        await base.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        base.release.set()
        result = await model.embed(("retained",))
        assert result.vectors == ((8.0,),)
        assert (await model.embed(("retained",))).usage.requests == 0
        assert base.calls == [("retained",)]

    asyncio.run(run())
