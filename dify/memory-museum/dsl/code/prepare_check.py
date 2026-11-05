import json


def _object(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def main(status_code: int, body: object) -> dict:
    data = _object(body)
    if data is None:
        return {
            "ok": "no",
            "memory_context": "",
            "memory_status": "invalid_response",
            "state_json": "{}",
            "error": "记忆服务返回了无法解析的数据，本轮不伪造旧展品。",
        }
    if not (200 <= int(status_code or 0) < 300) or data.get("ok") is not True:
        error = data.get("error") or {}
        message = error.get("message") if isinstance(error, dict) else ""
        return {
            "ok": "no",
            "memory_context": "",
            "memory_status": str(data.get("status") or "unavailable"),
            "state_json": "{}",
            "error": "记忆服务暂时不可用：" + (str(message or "未返回具体原因")),
        }

    status = str(data.get("status") or "empty")
    if status not in ("ready", "empty"):
        return {
            "ok": "no",
            "memory_context": "",
            "memory_status": status,
            "state_json": "{}",
            "error": "记忆服务返回了未知状态，本轮停止策展。",
        }
    context = data.get("memory_context")
    context = context if isinstance(context, str) else ""
    refs = data.get("memory_refs")
    refs = refs if isinstance(refs, list) else []
    return {
        "ok": "yes",
        "memory_context": context[:8000],
        "memory_status": status,
        "state_json": json.dumps(
            {
                "memory_status": status,
                "content_bytes": data.get("content_bytes") or len(context.encode("utf-8")),
                "memory_refs": refs,
            },
            ensure_ascii=False,
        ),
        "error": "",
    }
