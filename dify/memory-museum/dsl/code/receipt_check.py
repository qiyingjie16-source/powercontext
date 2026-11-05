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


def main(status_code: int, body: object, answer_markdown: str) -> dict:
    data = _object(body)
    if data is None:
        return {
            "ok": "no",
            "display": "",
            "error": "展品已经生成，但记忆写入结果无法解析；本轮不会伪称入藏成功。",
        }
    if not (200 <= int(status_code or 0) < 300) or data.get("ok") is not True:
        status = str(data.get("status") or "error")
        if status == "unknown":
            message = "PowerContext 没有返回确定的写入结果。为避免重复写入，本轮没有自动重试。"
        else:
            error = data.get("error") or {}
            detail = error.get("message") if isinstance(error, dict) else ""
            message = "展品已生成，但本轮入藏未成功：" + str(detail or status)
        return {
            "ok": "no",
            "display": answer_markdown.strip() + "\n\n---\n⚠️ " + message,
            "error": message,
        }

    records = data.get("records")
    artifact_id = ""
    if isinstance(records, list) and records and isinstance(records[0], dict):
        artifact = records[0].get("artifact")
        if isinstance(artifact, dict):
            artifact_id = str(artifact.get("artifact_id") or "")
    source = data.get("source")
    source_id = source.get("source_id") if isinstance(source, dict) else ""
    receipt = (
        "\n\n---\n"
        "🔐 灰灯馆长已把这件小事封存进 PowerContext。\n"
        "馆藏编号：`" + (artifact_id or "已返回成功") + "`\n"
        "来源编号：`" + (str(source_id) or "已返回来源回执") + "`"
    )
    return {
        "ok": "yes",
        "display": answer_markdown.strip() + receipt,
        "error": "",
    }
