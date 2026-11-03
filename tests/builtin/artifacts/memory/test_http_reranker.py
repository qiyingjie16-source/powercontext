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

"""HTTP reranker protocol and complete-pool ordering boundaries."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory.http_reranker import HTTPMemoryReranker
from powercontext.builtin.artifacts.memory.models import MemoryHit


def hit(rank: int) -> MemoryHit:
    return MemoryHit(
        memory_ref=ArtifactRef(family="memory", artifact_id="m", revision=1),
        entry_id=f"e{rank}",
        entry_version_id=f"v{rank}",
        text=f"record {rank}",
        score=1 / rank,
        matched_by=("fts",),
    )


def test_last_batch_global_ties_and_actual_request_count():
    requests = []
    scores = {1: 0.2, 2: 0.8, 3: 0.8, 4: 0.1, 5: 0.9}

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(
            200,
            json={
                "scores": [
                    {"rank": item["rank"], "score": scores[item["rank"]]} for item in reversed(body["candidates"])
                ]
            },
        )

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            decision = await HTTPMemoryReranker("https://rerank.invalid", client=client, batch_size=2).rerank(
                "evidence", tuple(hit(i) for i in range(1, 6)), 3
            )
        assert decision.selected_ranks == (5, 2, 3)
        assert decision.usage.requests == len(requests) == 3
        assert [len(request["candidates"]) for request in requests] == [2, 2, 1]
        assert [item for request in requests for item in request["candidates"]] == [
            {"rank": i, "text": hit(i).text} for i in range(1, 6)
        ]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b'{"scores":[{"rank":true,"score":0.5}]}',
        b'{"scores":[{"rank":1,"score":true}]}',
        b'{"scores":[{"rank":1,"score":"0.5"}]}',
        b'{"scores":[{"rank":1,"score":0.5}],"extra":0}',
        b'{"scores":[{"rank":1.0,"score":0.5}]}',
    ],
)
def test_malformed_score_responses_fail(body):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body))
        ) as client:
            with pytest.raises(ValueError):
                await HTTPMemoryReranker("https://rerank.invalid", client=client).rerank("q", (hit(1),), 1)

    asyncio.run(scenario())


def test_non_success_status_preserves_http_exception():
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(503))) as client:
            with pytest.raises(httpx.HTTPStatusError) as error:
                await HTTPMemoryReranker("https://rerank.invalid", client=client).rerank("q", (hit(1),), 1)
            assert error.value.response.status_code == 503

    asyncio.run(scenario())
