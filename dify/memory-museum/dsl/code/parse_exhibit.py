import json


def _text(value, limit):
    if not isinstance(value, str):
        value = str(value or "")
    value = " ".join(value.split()).strip()
    return value[:limit]


def _load_model_json(raw):
    candidate = raw.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        candidate = "\n".join(lines).strip()
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start >= 0 and end > start:
        candidate = candidate[start : end + 1]
    return json.loads(candidate)


def main(raw_llm: str, memory_text: str) -> dict:
    raw = raw_llm if isinstance(raw_llm, str) else ""
    source_memory = _text(memory_text, 700)
    try:
        exhibit = _load_model_json(raw)
    except Exception:
        exhibit = {}

    title = _text(exhibit.get("exhibit_title"), 40)
    if not title:
        title = "《一件还在路上的小事》"
    if not title.startswith("《"):
        title = "《" + title.strip("《》") + "》"

    era = _text(exhibit.get("era"), 40) or "无名纪元·刚刚发生"
    raw_artifacts = exhibit.get("artifacts")
    artifacts = []
    if isinstance(raw_artifacts, list):
        for item in raw_artifacts[:3]:
            cleaned = _text(item, 50)
            if cleaned:
                artifacts.append(cleaned)
    if not artifacts:
        artifacts = ["一段没有收据的日常", "一只拒绝解释的记忆抽屉"]

    note = _text(exhibit.get("curator_note"), 260)
    if not note:
        note = "它看起来没什么用。正因如此，本馆决定把它留下。"
    echo = _text(exhibit.get("visitor_echo"), 140)
    if not echo:
        echo = "下次带来相关线索时，看看旧展厅会不会先认出你。"
    echo_kind = _text(exhibit.get("echo_kind"), 16).lower()
    if echo_kind not in ("fresh", "sequel"):
        echo_kind = "fresh"
    echo_reference = _text(exhibit.get("echo_reference"), 60)

    artifact_lines = "\n".join("- " + item for item in artifacts)
    answer_markdown = (
        "### 🏛️ 新入藏：" + title + "\n\n"
        "**年代**：" + era + "\n\n"
        "> " + note + "\n\n"
        "**馆藏部件**\n" + artifact_lines + "\n\n"
        "**给你的回声**\n" + echo
    )
    memory = (
        "记忆失物招领处展品：" + title
        + "｜原始记忆：" + source_memory
        + "｜年代：" + era
        + "｜馆藏部件：" + "、".join(artifacts)
        + "｜馆长附注：" + note
        + "｜回声类型：" + echo_kind
        + "｜旧展品线索：" + (echo_reference or "无")
    )[:3000]
    description = _text(
        "记忆博物馆展品《" + title.strip("《》") + "》：" + source_memory,
        190,
    )
    return {
        "answer_markdown": answer_markdown,
        "memory_description": description,
        "memory_kind": "memory_museum_exhibit",
        "memory_text": memory,
        "memory_reason": "访客主动提交一件个人记忆，应用将其策展并保存为可再次检索的馆藏。",
    }
