from __future__ import annotations

import json

from fastapi.testclient import TestClient

from museum.app import create_app


class FakePowerContext:
    def __init__(self):
        self.memories: dict[str, list[str]] = {}

    def close(self):
        pass

    def create_scope(self, user_id: str) -> str:
        return "scope-" + user_id

    def prepare(self, scope_id: str, query: str, max_bytes: int):
        from museum.powercontext import PreparedMemory

        matches = [
            text
            for text in self.memories.get(scope_id, [])
            if any(token in text for token in query.split())
        ]
        content = "\n".join(matches)
        return PreparedMemory(
            status="ready" if content else "empty",
            content=content,
            content_bytes=len(content.encode()),
            refs=[],
        )

    def capture_source(self, scope_id: str, turn_id: str, content: str):
        return {
            "status": "accepted",
            "source": {"name": "content", "source_id": "museum-" + turn_id},
            "position": 1,
        }

    def remember(self, scope_id: str, **kwargs):
        self.memories.setdefault(scope_id, []).append(kwargs["text"])
        return {
            "changed": True,
            "records": [
                {
                    "artifact": {
                        "family": "atomic-memory",
                        "artifact_id": "art-" + scope_id,
                        "revision": 1,
                    }
                }
            ],
        }


def make_client(tmp_path):
    return TestClient(
        create_app(
            db_path=str(tmp_path / "museum.sqlite3"),
            service_key="test-key",
            powercontext_client=FakePowerContext(),
        )
    )


def test_service_auth_and_user_scope_isolation(tmp_path):
    with make_client(tmp_path) as client:
        assert client.get("/health").status_code == 200
        assert client.post("/v1/prepare", json={"user_id": "alice", "query": "香菜"}).status_code == 401

        headers = {"X-Museum-Key": "test-key"}
        alice = client.post(
            "/v1/prepare",
            headers=headers,
            json={"user_id": "alice", "query": "香菜"},
        ).json()
        bob = client.post(
            "/v1/prepare",
            headers=headers,
            json={"user_id": "bob", "query": "香菜"},
        ).json()
        assert alice["scope_id"] == "scope-alice"
        assert bob["scope_id"] == "scope-bob"
        assert alice["scope_id"] != bob["scope_id"]


def test_backend_blocks_sensitive_content_before_powercontext(tmp_path):
    with make_client(tmp_path) as client:
        headers = {"X-Museum-Key": "test-key"}
        blocked = client.post(
            "/v1/prepare",
            headers=headers,
            json={"user_id": "alice", "query": "api_key: sk-hidden-value"},
        ).json()
        assert blocked["ok"] is False
        assert blocked["status"] == "blocked"

        write = client.post(
            "/v1/remember",
            headers=headers,
            json={
                "user_id": "alice",
                "turn_id": "turn-sensitive",
                "kind": "memory_museum_exhibit",
                "text": "联系 someone@example.com",
            },
        ).json()
        assert write["ok"] is False
        assert write["status"] == "blocked"


def test_remember_then_recall_same_user(tmp_path):
    with make_client(tmp_path) as client:
        headers = {"X-Museum-Key": "test-key"}
        remember = client.post(
            "/v1/remember",
            headers=headers,
            json={
                "user_id": "alice",
                "turn_id": "turn-1",
                "kind": "memory_museum_exhibit",
                "text": "记忆失物招领处展品：《冰箱里的绿色书签》",
                "description": "一片香菜在冷冻层变成了绿色书签。",
                "reason": "测试显式写入。",
            },
        ).json()
        assert remember["ok"] is True
        assert remember["records"][0]["artifact"]["artifact_id"] == "art-scope-alice"

        recalled = client.post(
            "/v1/prepare",
            headers=headers,
            json={"user_id": "alice", "query": "绿色书签"},
        ).json()
        other = client.post(
            "/v1/prepare",
            headers=headers,
            json={"user_id": "bob", "query": "绿色书签"},
        ).json()
        assert recalled["status"] == "ready"
        assert "冰箱里的绿色书签" in recalled["memory_context"]
        assert other["status"] == "empty"


def test_openai_compatible_fixture_supports_streaming(tmp_path):
    with make_client(tmp_path) as client:
        response = client.post(
            "/model/v1/chat/completions",
            json={
                "model": "museum-fixture-v1",
                "messages": [
                    {
                        "role": "user",
                        "content": "当前失物：\n我把香菜忘在冰箱里。\nPowerContext 召回状态：fresh_gallery",
                    }
                ],
            },
        )
        exhibit = json.loads(response.json()["choices"][0]["message"]["content"])
        assert exhibit["echo_kind"] == "fresh"
        assert "香菜" in exhibit["curator_note"] or "书签" in exhibit["exhibit_title"]

        streamed = client.post(
            "/model/v1/chat/completions",
            json={
                "model": "museum-fixture-v1",
                "stream": True,
                "messages": [{"role": "user", "content": "当前失物：\n一件小事"}],
            },
        )
        assert streamed.status_code == 200
        assert "data: [DONE]" in streamed.text
