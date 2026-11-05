import re


_SENSITIVE_PATTERNS = (
    (
        r"(?i)(password|passwd|密码|口令|token|api[_ -]?key|secret)\s*[:=：]",
        "这段文字看起来包含密码、Token 或其他密钥。",
    ),
    (
        r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
        "这段文字包含邮箱地址。",
    ),
    (
        r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)",
        "这段文字包含手机号码。",
    ),
    (
        r"\b\d{17}[\dXx]\b",
        "这段文字包含身份证号。",
    ),
)


def main(text: str) -> dict:
    raw = text if isinstance(text, str) else ""
    compact = " ".join(raw.split()).strip()
    if not compact:
        return {
            "ok": "no",
            "reason": "馆长收到了一阵空白。请写下一件具体的小事，再放进失物招领箱。",
            "memory_text": "",
        }
    if len(compact) > 1200:
        return {
            "ok": "no",
            "reason": "这件失物太长，展柜放不下。请把小事压缩到 1200 个字符以内。",
            "memory_text": "",
        }
    for pattern, reason in _SENSITIVE_PATTERNS:
        if re.search(pattern, compact):
            return {
                "ok": "no",
                "reason": (
                    reason
                    + " 请换一件不包含隐私或凭据的小事；本次不会写入 PowerContext。"
                ),
                "memory_text": "",
            }
    return {
        "ok": "yes",
        "reason": "",
        "memory_text": compact,
    }
