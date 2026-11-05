import json


def main(
    user_id: str,
    turn_id: str,
    memory_kind: str,
    memory_text: str,
    memory_description: str,
    memory_reason: str,
) -> dict:
    text = str(memory_text or "").strip()
    if not text or len(text.encode("utf-8")) > 8192:
        return {
            "ok": "no",
            "body": "{}",
            "error": "展品记忆为空或超过 8192 字节，本轮没有提交写入。",
        }
    return {
        "ok": "yes",
        "body": json.dumps(
            {
                "user_id": str(user_id or "").strip(),
                "turn_id": str(turn_id or "").strip(),
                "kind": str(memory_kind or "memory_museum_exhibit"),
                "text": text,
                "description": str(memory_description or "")[:200] or None,
                "reason": str(memory_reason or "")[:512] or None,
            },
            ensure_ascii=False,
        ),
        "error": "",
    }
