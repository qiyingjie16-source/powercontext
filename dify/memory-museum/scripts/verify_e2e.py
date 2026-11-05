"""Drive the published Dify Chatflow and capture workflow-run evidence."""

from __future__ import annotations

import argparse
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from configure_dify import Console, login_local


class CheckFailed(RuntimeError):
    pass


def check(condition: bool, message: str, detail: Any = None) -> None:
    if not condition:
        raise CheckFailed(
            message if detail is None else f"{message}: {detail}"
        )


def json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


class Dify:
    def __init__(self, base: str, api_key: str, app_id: str, console: Console):
        self.base = base.rstrip("/")
        self.api_key = api_key
        self.app_id = app_id
        self.console = console

    def chat(self, user: str, query: str, conversation: str = "") -> dict[str, Any]:
        body = json.dumps(
            {
                "inputs": {},
                "query": query,
                "response_mode": "streaming",
                "user": user,
                "conversation_id": conversation,
            },
            ensure_ascii=False,
        ).encode()
        request = Request(
            self.base + "/v1/chat-messages",
            data=body,
            headers={
                "Authorization": "Bearer " + self.api_key,
                "Content-Type": "application/json",
            },
        )
        answer: list[str] = []
        events: list[dict[str, Any]] = []
        run_id = None
        conversation_id = conversation
        with urlopen(request, timeout=300) as response:
            for raw in response:
                line = raw.decode("utf-8").strip()
                if not line.startswith("data:"):
                    continue
                payload = json.loads(line[5:].strip())
                events.append(payload)
                event = payload.get("event")
                if event == "error":
                    raise CheckFailed("Dify returned a workflow error", payload)
                run_id = payload.get("workflow_run_id") or run_id
                conversation_id = payload.get("conversation_id") or conversation_id
                if event == "message" and payload.get("answer"):
                    answer.append(payload["answer"])
        check(bool(run_id), "Dify did not return workflow_run_id")
        nodes = self.nodes(str(run_id))
        return {
            "query": query,
            "answer": "".join(answer),
            "workflow_run_id": run_id,
            "conversation_id": conversation_id,
            "events": events,
            "nodes": nodes,
        }

    def nodes(self, run_id: str) -> dict[str, dict[str, Any]]:
        data = self.console.call(
            f"/apps/{self.app_id}/workflow-runs/{run_id}/node-executions"
        )
        items = data.get("data") if isinstance(data, dict) else data
        check(isinstance(items, list), "node execution response is not a list")
        return {
            str(item.get("title")): item.get("outputs") or {}
            for item in items
            if isinstance(item, dict)
        }

    def prepare_state(self, turn: dict[str, Any]) -> dict[str, Any]:
        outputs = turn["nodes"].get("召回旧展品（HTTP）")
        check(bool(outputs), "prepare HTTP node produced no output")
        result = json_object(outputs.get("body"))
        check(bool(result), "prepare HTTP response body is not a JSON object")
        return result

    def remember_receipt(self, turn: dict[str, Any]) -> dict[str, Any]:
        outputs = turn["nodes"].get("写入馆藏（HTTP）")
        check(bool(outputs), "remember HTTP node produced no output")
        result = json_object(outputs.get("body"))
        check(bool(result), "remember HTTP response body is not a JSON object")
        return result


def write_sse(path: Path, turn: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        for event in turn["events"]:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--base-url", default="http://localhost:18080")
    parser.add_argument("--state-file", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    root = args.root.resolve()
    project = Path(__file__).resolve().parents[1]
    state = json.loads(
        (args.state_file or root / "dify-local.json").read_text(encoding="utf-8")
    )
    check(bool(state.get("api_key")), "dify-local.json has no app API key")
    check(bool(state.get("app_id")), "dify-local.json has no app ID")

    console = Console(args.base_url)
    login_local(console, root)
    dify = Dify(args.base_url, state["api_key"], state["app_id"], console)
    user = "e2e-" + uuid.uuid4().hex[:12]
    other = "e2e-" + uuid.uuid4().hex[:12]
    evidence: dict[str, Any] = {
        "kind": "real_dify_engine_and_powercontext_e2e",
        "created_at": datetime.now(UTC).isoformat(),
        "dify_version": "1.17.1",
        "workflow_engine_executed": True,
        "model_executed": True,
        "model_execution_mode": "deterministic_fixture",
        "credentials_included": False,
        "user_id": user,
        "steps": [],
    }
    output = args.output or project / "evidence" / "dify-e2e.json"
    status = 1
    try:
        first = dify.chat(
            user,
            "我把一片香菜忘在冰箱冷冻层，第二天它硬得像一枚绿色书签。",
        )
        first_prepare = dify.prepare_state(first)
        first_saved = dify.remember_receipt(first)
        check(first_prepare.get("ok") is True, "first prepare failed", first_prepare)
        check(
            first_prepare.get("status") == "empty",
            "new user unexpectedly had existing memory",
            first_prepare.get("status"),
        )
        check(first_saved.get("ok") is True, "first memory write failed", first_saved)
        first_records = first_saved.get("records") or []
        check(bool(first_records), "first write returned no Artifact")
        first_artifact = first_records[0]["artifact"]["artifact_id"]
        check("新入藏" in first["answer"], "first answer has no exhibit receipt")
        write_sse(project / "evidence" / "ci-sse-session-1.jsonl", first)
        evidence["steps"].append(
            {
                "step": "session_1_curate",
                "conversation_id": first["conversation_id"],
                "workflow_run_id": first["workflow_run_id"],
                "prepare_status": first_prepare.get("status"),
                "remember_status": first_saved.get("status"),
                "artifact_id": first_artifact,
                "answer": first["answer"],
            }
        )

        second = dify.chat(
            user,
            "我又从冰箱拿出了那片香菜标本，它看起来像在等一个结局。",
        )
        second_prepare = dify.prepare_state(second)
        second_saved = dify.remember_receipt(second)
        check(
            second["conversation_id"] != first["conversation_id"],
            "second turn did not use a new conversation",
        )
        check(
            second_prepare.get("status") == "ready",
            "new conversation did not recall the first exhibit",
            second_prepare.get("status"),
        )
        context = second_prepare.get("memory_context") or ""
        check(
            "绿色书签" in context or "香菜" in context,
            "recalled context lost the first exhibit",
        )
        check(
            first_artifact in context,
            "recalled context lost the first Artifact citation",
        )
        check("绿色书签申请延期" in second["answer"], "fixture did not produce sequel")
        check(second_saved.get("ok") is True, "second memory write failed", second_saved)
        second_records = second_saved.get("records") or []
        check(bool(second_records), "second write returned no Artifact")
        second_artifact = second_records[0]["artifact"]["artifact_id"]
        write_sse(project / "evidence" / "ci-sse-session-2.jsonl", second)
        evidence["steps"].append(
            {
                "step": "new_session_recall_and_sequel",
                "conversation_id": second["conversation_id"],
                "workflow_run_id": second["workflow_run_id"],
                "prepare_status": second_prepare.get("status"),
                "content_bytes": second_prepare.get("content_bytes"),
                "memory_context": context,
                "remember_status": second_saved.get("status"),
                "artifact_id": second_artifact,
                "answer": second["answer"],
            }
        )

        isolated = dify.chat(other, "冰箱里的香菜绿色书签")
        isolated_prepare = dify.prepare_state(isolated)
        check(
            isolated_prepare.get("status") == "empty",
            "another Dify user saw the first user's PowerContext memory",
            isolated_prepare.get("status"),
        )
        evidence["steps"].append(
            {
                "step": "user_isolation",
                "other_user_id": other,
                "conversation_id": isolated["conversation_id"],
                "workflow_run_id": isolated["workflow_run_id"],
                "prepare_status": isolated_prepare.get("status"),
                "answer": isolated["answer"],
            }
        )
        evidence["result"] = "passed"
        status = 0
    except (CheckFailed, HTTPError, KeyError, ValueError) as error:
        evidence["result"] = "failed"
        evidence["error"] = f"{type(error).__name__}: {error}"
        print("FAILED:", evidence["error"], flush=True)
    evidence["finished_at"] = datetime.now(UTC).isoformat()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "result": evidence["result"],
                "output": str(output),
                "user_id": user,
                "credentials_included": False,
            },
            ensure_ascii=False,
        )
    )
    raise SystemExit(status)


if __name__ == "__main__":
    main()
