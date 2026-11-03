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

"""HTTP score-based reranking over immutable Memory candidate identities."""

from __future__ import annotations

import math
from typing import Any

import httpx

from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.artifacts.memory.reranking import MemoryRerankDecision
from powercontext.builtin.inference import InferenceUsage


class HTTPMemoryReranker:
    """Score bounded batches and select the best original global ranks."""

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient,
        batch_size: int = 32,
        policy_id: str = "http-cross-encoder-v1",
    ) -> None:
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size <= 0:
            raise ValueError("batch_size must be a positive integer")  # noqa: TRY003
        if not isinstance(policy_id, str) or not policy_id.strip():
            raise ValueError("policy_id must be non-empty")  # noqa: TRY003
        self.endpoint = endpoint
        self.client = client
        self.batch_size = batch_size
        self.policy_id = policy_id

    async def rerank(
        self,
        query: str,
        candidates: tuple[MemoryHit, ...],
        limit: int,
        /,
    ) -> MemoryRerankDecision:
        if not candidates or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= len(candidates):
            raise ValueError("limit must be between one and the candidate count")  # noqa: TRY003
        scores: dict[int, float] = {}
        requests = 0
        for start in range(0, len(candidates), self.batch_size):
            batch = candidates[start : start + self.batch_size]
            ranks = tuple(range(start + 1, start + 1 + len(batch)))
            response = await self.client.post(
                self.endpoint,
                json={
                    "query": query,
                    "candidates": [
                        {"rank": rank, "text": candidate.text} for rank, candidate in zip(ranks, batch, strict=True)
                    ],
                },
            )
            requests += 1
            response.raise_for_status()
            try:
                payload: Any = response.json()
            except ValueError as error:
                raise ValueError("reranker response must be JSON") from error  # noqa: TRY003
            if not isinstance(payload, dict) or set(payload) != {"scores"} or not isinstance(payload["scores"], list):
                raise ValueError("reranker response must contain only a scores list")  # noqa: TRY003
            batch_scores: dict[int, float] = {}
            for item in payload["scores"]:
                if not isinstance(item, dict) or set(item) != {"rank", "score"}:
                    raise ValueError("each score requires exactly rank and score")  # noqa: TRY003
                rank, score = item["rank"], item["score"]
                if type(rank) is not int or rank not in ranks or rank in batch_scores:
                    raise ValueError("response ranks must cover this batch exactly once")  # noqa: TRY003
                if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
                    raise ValueError("reranker scores must be finite numbers")  # noqa: TRY003
                batch_scores[rank] = float(score)
            if set(batch_scores) != set(ranks):
                raise ValueError("response ranks must cover this batch exactly once")  # noqa: TRY003
            scores.update(batch_scores)
        selected = tuple(sorted(scores, key=lambda rank: (-scores[rank], rank))[:limit])
        return MemoryRerankDecision(selected_ranks=selected, usage=InferenceUsage(requests=requests))


__all__ = ["HTTPMemoryReranker"]
