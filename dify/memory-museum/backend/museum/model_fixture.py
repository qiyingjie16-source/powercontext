from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse


router = APIRouter(prefix="/model/v1")
MODEL_NAME = "museum-fixture-v1"


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return ""


def _current_memory(messages: list[dict[str, Any]]) -> str:
    all_text = "\n".join(_message_text(message.get("content")) for message in messages)
    if "当前失物：" in all_text:
        candidate = all_text.split("当前失物：", 1)[1]
    else:
        candidate = all_text
    candidate = candidate.split("PowerContext 召回状态：", 1)[0]
    candidate = candidate.split("旧展品检索结果", 1)[0]
    candidate = candidate.split("请执行策展", 1)[0]
    lines = [line.strip() for line in candidate.splitlines() if line.strip()]
    return (lines[0] if lines else "一件还没说出口的小事")[:500]


def _previous_title(messages: list[dict[str, Any]]) -> str:
    all_text = "\n".join(_message_text(message.get("content")) for message in messages)
    matches = re.findall(r"记忆失物招领处展品：《([^》]+)》", all_text)
    return matches[-1] if matches else ""


def _exhibit(messages: list[dict[str, Any]]) -> dict[str, str]:
    memory = _current_memory(messages)
    previous = _previous_title(messages)
    if previous:
        return {
            "exhibit_title": "《绿色书签申请延期》",
            "era": "冰箱纪·第二次结霜",
            "artifacts": ["一片香菜", "一台装作不知情的冰箱", "一份迟到的结局申请书"],
            "curator_note": (
                "旧展柜认出了《"
                + previous
                + "》。那片香菜没有失踪，它只是把第一次展览延期了。"
            ),
            "visitor_echo": "上一件展品还在等你给它补一个结局。",
            "echo_kind": "sequel",
            "echo_reference": "《" + previous + "》",
        }
    if "香菜" in memory or "冰箱" in memory:
        title = "《冰箱里的绿色书签》"
        artifacts = ["一片香菜", "冷冻层", "一枚绿色书签"]
        note = "它没有坏，只是选择了另一种时间，并顺手成为了一枚书签。"
    else:
        title = "《刚刚发出声音的小事》"
        artifacts = ["一段没有收据的日常", "一只安静的展柜", "一枚无名标签"]
        note = "它看起来没什么用。正因如此，本馆决定把它留下。"
    return {
        "exhibit_title": title,
        "era": "无名纪元·刚刚发生",
        "artifacts": artifacts,
        "curator_note": note,
        "visitor_echo": "下次带来相关线索时，看看旧展厅会不会先认出你。",
        "echo_kind": "fresh",
        "echo_reference": "",
    }


def _completion_body(payload: dict[str, Any]) -> dict[str, Any]:
    content = json.dumps(_exhibit(payload.get("messages") or []), ensure_ascii=False)
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": payload.get("model") or MODEL_NAME,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 1,
            "completion_tokens": max(1, len(content) // 4),
            "total_tokens": 1 + max(1, len(content) // 4),
        },
    }


def _stream(payload: dict[str, Any]):
    completion = _completion_body(payload)
    identifier = completion["id"]
    created = completion["created"]
    model = completion["model"]
    content = completion["choices"][0]["message"]["content"]
    first = {
        "id": identifier,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
    }
    second = {
        "id": identifier,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
    }
    final = {
        "id": identifier,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "usage": completion["usage"],
    }
    for event in (first, second, final):
        yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
    yield "data: [DONE]\n\n"


@router.get("/models")
def models() -> dict[str, Any]:
    return {
        "object": "list",
        "data": [
            {
                "id": MODEL_NAME,
                "object": "model",
                "created": 0,
                "owned_by": "memory-museum",
            }
        ],
    }


@router.get("/models/{model_id}")
def model(model_id: str) -> JSONResponse:
    return JSONResponse(
        {
            "id": model_id,
            "object": "model",
            "created": 0,
            "owned_by": "memory-museum",
        }
    )


@router.post("/chat/completions")
async def chat_completions(request: Request):
    payload = await request.json()
    if not isinstance(payload, dict):
        return JSONResponse({"error": {"message": "request must be an object"}}, status_code=422)
    if payload.get("stream"):
        return StreamingResponse(
            _stream(payload),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    return JSONResponse(_completion_body(payload))
