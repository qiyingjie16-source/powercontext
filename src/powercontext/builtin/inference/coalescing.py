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

"""Loop-local shared embedding requests and bounded successful-vector reuse."""

from __future__ import annotations

import asyncio
import math
from collections import OrderedDict
from dataclasses import dataclass

from powercontext.builtin.inference.models import EmbeddingResult, EmbeddingVector, InferenceUsage
from powercontext.builtin.inference.protocols import EmbeddingModel


@dataclass(eq=False, slots=True)
class _BatchResult:
    result: EmbeddingResult | None = None
    error: BaseException | None = None


_Ticket = asyncio.Future[tuple[_BatchResult, int]]


class CoalescingEmbeddingModel:
    """Share pending text computations, retaining only successful vectors in an LRU.

    Use one wrapper per model profile and event loop. Cancelling a caller leaves
    provider work alive for other callers and successful cache population.
    """

    def __init__(
        self,
        model: EmbeddingModel,
        *,
        max_batch_size: int = 64,
        coalesce_ms: float = 5.0,
        cache_size: int = 1024,
    ) -> None:
        if type(max_batch_size) is not int or max_batch_size <= 0:
            raise ValueError("max_batch_size must be a positive integer")  # noqa: TRY003
        if type(cache_size) is not int or cache_size < 0:
            raise ValueError("cache_size must be a nonnegative integer")  # noqa: TRY003
        if (
            isinstance(coalesce_ms, bool)
            or not isinstance(coalesce_ms, (float, int))
            or not math.isfinite(coalesce_ms)
            or coalesce_ms < 0
        ):
            raise ValueError("coalesce_ms must be finite and nonnegative")  # noqa: TRY003
        self.profile = model.profile
        self._model = model
        self._max_batch_size = max_batch_size
        self._delay = coalesce_ms / 1000
        self._cache_size = cache_size
        self._cache: OrderedDict[str, EmbeddingVector] = OrderedDict()
        self._inflight: dict[str, _Ticket] = {}
        self._pending: dict[str, _Ticket] = {}
        self._flush_task: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        """Return original order and charge each awaited provider batch once."""
        vectors: dict[str, EmbeddingVector] = {}
        tickets: dict[str, _Ticket] = {}
        loop = asyncio.get_running_loop()
        # Registration has no suspension: concurrent callers see the same tickets.
        for text in texts:
            if text in vectors or text in tickets:
                continue
            if text in self._cache:
                vectors[text] = self._cache[text]
                self._cache.move_to_end(text)
                continue
            ticket = self._inflight.get(text)
            if ticket is None:
                ticket = loop.create_future()
                self._inflight[text] = ticket
                self._pending[text] = ticket
            tickets[text] = ticket
        if self._pending and self._flush_task is None:
            self._flush_task = asyncio.create_task(self._flush())
        outcomes = await asyncio.gather(*(asyncio.shield(ticket) for ticket in tickets.values()))
        batches: dict[_BatchResult, InferenceUsage] = {}
        for text, (batch, index) in zip(tickets, outcomes, strict=True):
            if batch.error is not None:
                raise batch.error
            assert batch.result is not None  # noqa: S101 - internal successful batch invariant
            vectors[text] = batch.result.vectors[index]
            batches[batch] = batch.result.usage
        usages = tuple(batches.values())
        return EmbeddingResult(
            vectors=tuple(vectors[text] for text in texts),
            usage=InferenceUsage(
                requests=sum(usage.requests for usage in usages),
                input_tokens=_sum_tokens(tuple(usage.input_tokens for usage in usages)),
                output_tokens=_sum_tokens(tuple(usage.output_tokens for usage in usages)),
            ),
        )

    async def _flush(self) -> None:
        await asyncio.sleep(self._delay)
        pending = self._pending
        self._pending = {}
        self._flush_task = None
        texts = tuple(pending)
        for start in range(0, len(texts), self._max_batch_size):
            batch = texts[start : start + self._max_batch_size]
            task = asyncio.create_task(self._run_batch(batch, pending))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _run_batch(self, texts: tuple[str, ...], tickets: dict[str, _Ticket]) -> None:
        outcome = _BatchResult()
        try:
            result = await self._model.embed(texts)
            if len(result.vectors) != len(texts):
                raise ValueError("provider vector count does not match batch")  # noqa: TRY003, TRY301 - normalize provider shape failure for waiters
            outcome.result = result
            if self._cache_size:
                for text, vector in zip(texts, result.vectors, strict=True):
                    self._cache[text] = vector
                    self._cache.move_to_end(text)
                    while len(self._cache) > self._cache_size:
                        self._cache.popitem(last=False)
        except (Exception, asyncio.CancelledError) as error:
            # Deliver failures as values so abandoned callers never leave an
            # unobserved Future exception; only dependent callers raise it.
            outcome.error = error
        finally:
            for index, text in enumerate(texts):
                del self._inflight[text]
                tickets[text].set_result((outcome, index))


def _sum_tokens(values: tuple[int | None, ...]) -> int | None:
    return None if all(value is None for value in values) else sum(value or 0 for value in values)


__all__ = ["CoalescingEmbeddingModel"]
