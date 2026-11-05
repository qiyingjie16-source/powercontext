from __future__ import annotations

import re


_PATTERNS = (
    re.compile(r"(?i)\b(?:password|passwd|api[_ -]?key|secret|token)\s*[:=：]"),
    re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"),
    re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)"),
    re.compile(r"\b\d{17}[\dXx]\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)


def sensitive_reason(value: str) -> str | None:
    """Return a safe reason when obvious credentials or identifiers are present."""

    if len(value.encode("utf-8")) > 12000:
        return "输入超过服务端安全长度限制。"
    if any(pattern.search(value) for pattern in _PATTERNS):
        return "输入包含疑似密码、Token、邮箱、手机号、身份证号或私钥。"
    return None
