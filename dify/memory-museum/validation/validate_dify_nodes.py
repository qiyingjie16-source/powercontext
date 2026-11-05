from __future__ import annotations

import argparse
import importlib.metadata
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from graphon.nodes.answer.entities import AnswerNodeData
from graphon.nodes.code.entities import CodeNodeData
from graphon.nodes.http_request.entities import HttpRequestNodeData
from graphon.nodes.if_else.entities import IfElseNodeData
from graphon.nodes.llm.entities import LLMNodeData
from graphon.nodes.start.entities import StartNodeData
from graphon.nodes.tool.entities import ToolNodeData


NODE_MODELS = {
    "answer": AnswerNodeData,
    "code": CodeNodeData,
    "http-request": HttpRequestNodeData,
    "if-else": IfElseNodeData,
    "llm": LLMNodeData,
    "start": StartNodeData,
    "tool": ToolNodeData,
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate this DSL with the Graphon models shipped by Dify 1.17.1."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "evidence"
        / "dify-node-schema.json",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    dsl_path = root / "dsl" / "memory-museum.yml"
    document = yaml.safe_load(dsl_path.read_text(encoding="utf-8"))
    nodes = document["workflow"]["graph"]["nodes"]
    validated: list[dict[str, Any]] = []

    for node in nodes:
        node_id = str(node["id"])
        node_data = node["data"]
        node_type = str(node_data["type"])
        model = NODE_MODELS[node_type]
        parsed = model.model_validate(node_data)
        validated.append(
            {
                "node_id": node_id,
                "node_type": node_type,
                "graphon_model": model.__name__,
                "validated": True,
            }
        )
        print(f"{node_id}: {model.__name__} ok")

    evidence = {
        "schema": "memory-museum.dify-node-schema.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "target_dify_version": "1.17.1",
        "graphon_version": importlib.metadata.version("graphon"),
        "dsl_path": str(dsl_path.relative_to(root)),
        "node_count": len(validated),
        "nodes": validated,
        "result": "passed",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "result": "passed",
                "graphon_version": evidence["graphon_version"],
                "nodes": len(validated),
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
