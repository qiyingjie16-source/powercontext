from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import defaultdict, deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml


EXPECTED_PLUGIN = (
    "langgenius/openai_api_compatible:0.0.72@"
    "87f3c4c7acee9ff03ac2c04ba8191edc5ea87cd53e36af1bbf1ccd13f00352a4"
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check(
    checks: list[dict[str, Any]],
    name: str,
    condition: bool,
    detail: str,
) -> None:
    checks.append({"name": name, "ok": bool(condition), "detail": detail})
    if not condition:
        raise AssertionError(f"{name}: {detail}")


def reachable(edges: list[dict[str, Any]], start: str) -> set[str]:
    by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for edge in edges:
        by_source[str(edge["source"])].append(edge)
    visited = {start}
    pending = deque([start])
    while pending:
        source = pending.popleft()
        for edge in by_source.get(source, []):
            target = str(edge["target"])
            if target not in visited:
                visited.add(target)
                pending.append(target)
    return visited


def validate_dsl(
    checks: list[dict[str, Any]],
    path: Path,
    *,
    expected_model: str,
) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    document = yaml.safe_load(text)
    check(checks, f"{path.name}:yaml", isinstance(document, dict), "valid YAML mapping")
    check(checks, f"{path.name}:kind", document.get("kind") == "app", "kind=app")
    check(
        checks,
        f"{path.name}:version",
        document.get("version") == "0.7.0",
        "Dify 1.17.1-compatible DSL version 0.7.0",
    )
    app = document.get("app") or {}
    check(
        checks,
        f"{path.name}:chatflow",
        app.get("mode") == "advanced-chat",
        "advanced-chat mode",
    )
    workflow = document["workflow"]
    graph = workflow["graph"]
    nodes = graph["nodes"]
    edges = graph["edges"]
    node_by_id = {str(node["id"]): node for node in nodes}
    check(
        checks,
        f"{path.name}:unique_nodes",
        len(node_by_id) == len(nodes),
        f"{len(nodes)} unique nodes",
    )

    for edge in edges:
        source = str(edge["source"])
        target = str(edge["target"])
        check(
            checks,
            f"{path.name}:endpoints:{edge['id']}",
            source in node_by_id and target in node_by_id,
            f"{source} -> {target}",
        )
        check(
            checks,
            f"{path.name}:source_type:{edge['id']}",
            edge["data"]["sourceType"] == node_by_id[source]["data"]["type"],
            edge["data"]["sourceType"],
        )
        check(
            checks,
            f"{path.name}:target_type:{edge['id']}",
            edge["data"]["targetType"] == node_by_id[target]["data"]["type"],
            edge["data"]["targetType"],
        )

    reachable_ids = reachable(edges, "start")
    required = {
        "prepare_http",
        "remember_http",
        "curator_llm",
        "answer",
        "prepare_error",
        "receipt_error",
        "transport_error",
        "privacy_answer",
    }
    check(
        checks,
        f"{path.name}:reachability",
        required.issubset(reachable_ids),
        "success and failure paths reachable",
    )

    http_nodes = [
        node for node in nodes if node["data"].get("type") == "http-request"
    ]
    endpoints = {node["data"].get("url") for node in http_nodes}
    check(
        checks,
        f"{path.name}:http_nodes",
        len(http_nodes) == 2
        and endpoints
        == {
            "http://museum:8080/v1/prepare",
            "http://museum:8080/v1/remember",
        },
        f"found {sorted(str(item) for item in endpoints)}",
    )
    for node in http_nodes:
        data = node["data"]
        check(
            checks,
            f"{path.name}:http_auth:{node['id']}",
            "X-Museum-Key:{{#env.MUSEUM_SERVICE_KEY#}}" in data.get("headers", ""),
            "service key is injected through a Dify secret environment variable",
        )
        check(
            checks,
            f"{path.name}:http_no_retry:{node['id']}",
            data.get("retry_config", {}).get("retry_enabled") is False,
            "unknown writes are never retried automatically",
        )

    llm_nodes = [
        node for node in nodes if node["data"].get("type") == "llm"
    ]
    check(
        checks,
        f"{path.name}:llm_count",
        len(llm_nodes) == 1,
        f"{len(llm_nodes)} LLM nodes",
    )
    llm = llm_nodes[0]["data"]
    check(
        checks,
        f"{path.name}:llm_model",
        llm["model"]["name"] == expected_model,
        llm["model"]["name"],
    )
    check(
        checks,
        f"{path.name}:llm_context",
        llm.get("context", {}).get("enabled") is True
        and llm["context"].get("variable_selector")
        == ["prepare_check", "memory_context"],
        "LLM context is wired to the checked PowerContext response",
    )

    code_nodes = [
        node for node in nodes if node["data"].get("type") == "code"
    ]
    for node in code_nodes:
        compile(str(node["data"]["code"]), str(node["id"]), "exec")
    check(
        checks,
        f"{path.name}:code_compile",
        len(code_nodes) == 6,
        f"{len(code_nodes)} code nodes compile",
    )

    environment = {
        item.get("name"): item for item in workflow.get("environment_variables", [])
    }
    service_key = environment.get("MUSEUM_SERVICE_KEY", {})
    check(
        checks,
        f"{path.name}:secret_env",
        service_key.get("value_type") == "secret" and service_key.get("value") == "",
        "service key is a secret placeholder, not a committed value",
    )

    dependencies = document.get("dependencies") or []
    dependency_values = [
        item.get("value", {}).get("marketplace_plugin_unique_identifier")
        for item in dependencies
    ]
    check(
        checks,
        f"{path.name}:model_dependency",
        EXPECTED_PLUGIN in dependency_values,
        "OpenAI-compatible plugin is pinned",
    )

    credential_patterns = {
        "OpenAI secret": r"\bsk-[A-Za-z0-9_-]{20,}\b",
        "literal bearer token": (
            r"Authorization:\s*Bearer\s+(?!<|TOKEN|replace)[A-Za-z0-9._-]{16,}"
        ),
        "literal museum key": (
            r"MUSEUM_SERVICE_KEY=[A-Za-z0-9._-]{12,}"
        ),
    }
    found = [
        label
        for label, pattern in credential_patterns.items()
        if re.search(pattern, text, flags=re.IGNORECASE)
    ]
    check(
        checks,
        f"{path.name}:no_credentials",
        not found,
        "no credential-shaped values",
    )
    return {
        "path": str(path.relative_to(path.parents[2])),
        "sha256": sha256(path),
        "nodes": len(nodes),
        "edges": len(edges),
        "model": llm["model"]["name"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate Memory Museum submission files.")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "evidence"
        / "submission-validation.json",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    checks: list[dict[str, Any]] = []
    evidence: dict[str, Any] = {
        "schema": "memory-museum.submission-validation.v2",
        "generated_at": datetime.now(UTC).isoformat(),
        "checks": checks,
        "result": "running",
    }
    try:
        primary = validate_dsl(
            checks,
            root / "dsl" / "memory-museum.yml",
            expected_model="gpt-4.1-mini",
        )
        e2e = validate_dsl(
            checks,
            root / "dsl" / "memory-museum.e2e.yml",
            expected_model="museum-fixture-v1",
        )
        check(
            checks,
            "backend_lockfile",
            (root / "backend" / "uv.lock").is_file(),
            "backend dependency lock exists",
        )
        check(
            checks,
            "compose_files",
            (root / "deploy" / "compose.yaml").is_file()
            and (root / "deploy" / "dify.override.yaml").is_file(),
            "Dify and museum Compose definitions exist",
        )
        check(
            checks,
            "real_e2e_script",
            (root / "scripts" / "verify_e2e.py").is_file(),
            "workflow_run_id and node-execution verifier exists",
        )
        evidence["primary_dsl"] = primary
        evidence["e2e_dsl"] = e2e
        evidence["backend_files"] = [
            "backend/museum/app.py",
            "backend/museum/powercontext.py",
            "backend/museum/model_fixture.py",
        ]
        evidence["result"] = "passed"
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "result": "passed",
                    "checks": len(checks),
                    "primary_nodes": primary["nodes"],
                    "e2e_model": e2e["model"],
                    "output": str(args.output),
                },
                ensure_ascii=False,
            )
        )
        return 0
    except Exception as error:
        evidence["result"] = "failed"
        evidence["error"] = str(error)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"Submission validation failed: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
