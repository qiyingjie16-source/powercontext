import json
import re
import uuid


def main(user_id: str, conversation_id: str, turn_id: str, query: str) -> dict:
    user = str(user_id or "").strip()
    if len(user) < 3 or len(user) > 128 or not re.fullmatch(r"[A-Za-z0-9._:@-]+", user):
        user = "web-user-" + uuid.uuid4().hex[:12]
    turn = str(turn_id or "").strip() or uuid.uuid4().hex
    conversation = str(conversation_id or "").strip()[:256]
    clean_query = " ".join(str(query or "").split()).strip()[:2000]
    return {
        "body": json.dumps(
            {
                "user_id": user,
                "conversation_id": conversation,
                "turn_id": turn,
                "query": clean_query,
                "max_bytes": 8000,
            },
            ensure_ascii=False,
        )
    }
