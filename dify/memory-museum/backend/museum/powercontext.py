from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

import httpx


class PowerContextError(RuntimeError):
    def __init__(self, code: str, message: str, *, uncertain: bool = False):
        super().__init__(message)
        self.code = code
        self.uncertain = uncertain


@dataclass(frozen=True)
class PreparedMemory:
    status: str
    content: str
    content_bytes: int
    refs: list[dict[str, Any]]


def extract_atomic_refs(content: str) -> list[dict[str, Any]]:
    marker = "BEGIN_POWERCONTEXT_PREPARED_CONTEXT_V1\n"
    end_marker = "\nEND_POWERCONTEXT_PREPARED_CONTEXT_V1"
    begin = content.find(marker)
    end = content.find(end_marker)
    if begin < 0 or end < 0:
        return []
    try:
        envelope = json.loads(content[begin + len(marker) : end])
    except ValueError:
        return []

    refs: list[dict[str, Any]] = []
    for item in envelope.get("items", []):
        citation = item.get("citation") or {}
        ref = citation.get("artifact_ref")
        if not isinstance(ref, dict):
            ref = (citation.get("artifact") or {}).get("artifact")
        if (
            isinstance(ref, dict)
            and ref.get("family") == "atomic-memory"
            and isinstance(ref.get("artifact_id"), str)
            and type(ref.get("revision")) is int
        ):
            refs.append(
                {
                    "family": "atomic-memory",
                    "artifact_id": ref["artifact_id"],
                    "revision": ref["revision"],
                }
            )
    return refs


class PowerContextClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        transport: httpx.BaseTransport | None = None,
    ):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=httpx.Timeout(12.0, connect=3.0),
            transport=transport,
        )

    def close(self) -> None:
        self.client.close()

    def _post(self, path: str, payload: dict[str, Any], *, write: bool = False) -> dict[str, Any]:
        try:
            response = self.client.post(path, json=payload)
            response.raise_for_status()
        except httpx.TimeoutException as error:
            raise PowerContextError(
                "powercontext_timeout",
                "PowerContext did not answer before the service timeout.",
                uncertain=write,
            ) from error
        except httpx.HTTPStatusError as error:
            raise PowerContextError(
                "powercontext_http_error",
                f"PowerContext returned HTTP {error.response.status_code}.",
            ) from error
        except httpx.HTTPError as error:
            raise PowerContextError(
                "powercontext_unreachable",
                "PowerContext is unreachable.",
                uncertain=write,
            ) from error
        try:
            data = response.json()
        except ValueError as error:
            raise PowerContextError(
                "powercontext_invalid_json",
                "PowerContext returned invalid JSON.",
                uncertain=write,
            ) from error
        if not isinstance(data, dict):
            raise PowerContextError(
                "powercontext_invalid_response",
                "PowerContext returned a non-object response.",
                uncertain=write,
            )
        return data

    def create_scope(self, user_id: str) -> str:
        idempotency_key = "memory-museum-" + hashlib.sha256(user_id.encode()).hexdigest()
        data = self._post(
            "/v1/scopes",
            {
                "title": "记忆失物招领处 " + idempotency_key[-12:],
                "summary": "Memory Museum exhibits isolated by the Dify user identity.",
                "idempotency_key": idempotency_key,
            },
        )
        scope_id = data.get("scope_id")
        if not isinstance(scope_id, str) or not scope_id:
            raise PowerContextError(
                "powercontext_missing_scope",
                "PowerContext did not return a Scope ID.",
            )
        return scope_id

    def prepare(self, scope_id: str, query: str, max_bytes: int) -> PreparedMemory:
        data = self._post(
            "/v1/context/prepare",
            {
                "scope_id": scope_id,
                "query": "记忆失物招领处 " + query,
                "max_bytes": max_bytes,
            },
        )
        status = data.get("status")
        if status not in ("ready", "empty"):
            raise PowerContextError(
                "powercontext_invalid_context",
                "PowerContext returned an unexpected PreparedContext status.",
            )
        content = data.get("content")
        if status == "ready" and not isinstance(content, str):
            raise PowerContextError(
                "powercontext_missing_context",
                "PowerContext returned ready without content.",
            )
        content = content if isinstance(content, str) else ""
        content_bytes = data.get("content_bytes")
        if type(content_bytes) is not int:
            content_bytes = len(content.encode("utf-8"))
        return PreparedMemory(
            status=status,
            content=content,
            content_bytes=content_bytes,
            refs=extract_atomic_refs(content),
        )

    def capture_source(self, scope_id: str, turn_id: str, content: str) -> dict[str, Any]:
        source_key = "museum-" + hashlib.sha256(turn_id.encode()).hexdigest()[:32]
        data = self._post(
            "/v1/sources/content",
            {
                "scope_id": scope_id,
                "source_id": source_key,
                "content": content,
                "metadata": {
                    "integration": "memory-museum",
                    "turn_id": hashlib.sha256(turn_id.encode()).hexdigest(),
                },
            },
            write=True,
        )
        source = data.get("source")
        if (
            data.get("status") != "accepted"
            or not isinstance(source, dict)
            or source.get("source_id") != source_key
            or type(data.get("position")) is not int
        ):
            raise PowerContextError(
                "powercontext_invalid_source_receipt",
                "PowerContext did not return a valid Source receipt.",
                uncertain=True,
            )
        return data

    def remember(
        self,
        scope_id: str,
        *,
        kind: str,
        text: str,
        description: str | None,
        reason: str | None,
        source: dict[str, Any],
    ) -> dict[str, Any]:
        citation = json.dumps(
            {
                "source": source["source"],
                "position": source["position"],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        data = self._post(
            "/v1/memory/remember",
            {
                "scope_id": scope_id,
                "kind": kind,
                "text": text + "\nSource audit citation: " + citation,
                "description": description,
                "reason": reason,
            },
            write=True,
        )
        records = data.get("records")
        if not isinstance(records, list) or not records:
            raise PowerContextError(
                "powercontext_missing_memory",
                "PowerContext did not return an Atomic Memory record.",
                uncertain=True,
            )
        for record in records:
            artifact = record.get("artifact") if isinstance(record, dict) else None
            if (
                not isinstance(artifact, dict)
                or artifact.get("family") != "atomic-memory"
                or not isinstance(artifact.get("artifact_id"), str)
                or type(artifact.get("revision")) is not int
            ):
                raise PowerContextError(
                    "powercontext_invalid_memory_ref",
                    "PowerContext returned an invalid Atomic Memory reference.",
                    uncertain=True,
                )
        return data
