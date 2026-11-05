"""Initialize local Dify, install the model plugin, import, and publish the DSL."""

from __future__ import annotations

import argparse
import base64
import http.cookiejar
import json
import secrets
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener

import yaml


PLUGIN = (
    "langgenius/openai_api_compatible:0.0.72@"
    "87f3c4c7acee9ff03ac2c04ba8191edc5ea87cd53e36af1bbf1ccd13f00352a4"
)
PROVIDER = "langgenius/openai_api_compatible/openai_api_compatible"
FIXTURE_MODEL = "museum-fixture-v1"
FIXTURE_ENDPOINT = "http://museum:8080/model/v1"


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


class Console:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.jar))

    def call(
        self,
        path: str,
        payload: dict[str, Any] | None = None,
        method: str | None = None,
    ) -> Any:
        headers = {"Content-Type": "application/json", "Origin": self.base}
        for cookie in self.jar:
            if cookie.name.removeprefix("__Host-") == "csrf_token":
                headers["X-CSRF-Token"] = cookie.value
        request = Request(
            self.base + "/console/api" + path,
            data=None if payload is None else json.dumps(payload, ensure_ascii=False).encode(),
            headers=headers,
            method=method,
        )
        try:
            with self.opener.open(request, timeout=120) as response:
                body = response.read()
                return json.loads(body) if body else {}
        except HTTPError as error:
            raise RuntimeError(
                f"Dify {method or ('POST' if payload is not None else 'GET')} "
                f"{path}: HTTP {error.code}"
            ) from None


def login_local(console: Console, root: Path) -> dict[str, str]:
    credentials_path = root / "local-credentials.json"
    setup = console.call("/setup")
    if setup["step"] == "not_started":
        if credentials_path.exists():
            account = json.loads(credentials_path.read_text(encoding="utf-8"))
        else:
            account = {
                "email": "owner@example.com",
                "password": "Pw9!" + secrets.token_urlsafe(24),
            }
            credentials_path.write_text(
                json.dumps(account, indent=2) + "\n",
                encoding="utf-8",
            )
            credentials_path.chmod(0o600)
        console.call(
            "/setup",
            {
                **account,
                "name": "记忆失物招领处馆长",
                "language": "zh-Hans",
            },
        )
    if not credentials_path.exists():
        raise SystemExit(
            "Dify is already initialized. Provide local-credentials.json; "
            "existing accounts are not modified."
        )
    account = json.loads(credentials_path.read_text(encoding="utf-8"))
    result = console.call(
        "/login",
        {
            **account,
            "password": base64.b64encode(account["password"].encode()).decode(),
            "remember_me": True,
        },
    )
    if result.get("result") != "success":
        raise RuntimeError("Dify console login did not succeed")
    return account


def wait_http(url: str, *, timeout: float = 600.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with build_opener().open(Request(url), timeout=5) as response:
                if response.status < 500:
                    return
        except Exception as error:
            last_error = error
        time.sleep(3)
    raise RuntimeError(f"Timed out waiting for {url}: {last_error}")


def wait_provider(console: Console) -> None:
    for _ in range(60):
        providers = console.call("/workspaces/current/model-providers")
        items = providers.get("data") if isinstance(providers, dict) else providers
        if isinstance(items, list) and any(
            item.get("provider") == PROVIDER for item in items
        ):
            return
        time.sleep(2)
    raise RuntimeError("OpenAI-API-compatible provider did not become available")


def configure_fixture_model(console: Console) -> None:
    console.call(
        f"/workspaces/current/model-providers/{PROVIDER}/models/credentials",
        {
            "model": FIXTURE_MODEL,
            "model_type": "llm",
            "name": "Memory Museum deterministic E2E fixture",
            "credentials": {
                "api_key": "museum-fixture-key",
                "endpoint_url": FIXTURE_ENDPOINT,
                "mode": "chat",
                "context_size": "32768",
                "max_tokens_to_sample": "4096",
                "agent_thought_support": "not_supported",
                "function_calling_type": "no_call",
                "vision_support": "no_support",
                "structured_output_support": "not_supported",
                "stream_include_usage": "enabled",
                "api_type": "chat_completions",
            },
        },
    )


def import_app(
    *,
    console: Console,
    root: Path,
    project: Path,
    dsl_path: Path,
    new_app: bool,
    evidence_name: str,
) -> dict[str, Any]:
    dsl = yaml.safe_load(dsl_path.read_text(encoding="utf-8"))
    service_key = read_env(project / "deploy" / ".env")["MUSEUM_SERVICE_KEY"]
    found = False
    for variable in dsl["workflow"].get("environment_variables", []):
        if variable.get("name") == "MUSEUM_SERVICE_KEY":
            variable["value"] = service_key
            found = True
    if not found:
        raise SystemExit("DSL is missing MUSEUM_SERVICE_KEY")

    state_file = root / "dify-local.json"
    state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
    request: dict[str, Any] = {
        "mode": "yaml-content",
        "yaml_content": yaml.safe_dump(
            dsl,
            allow_unicode=True,
            sort_keys=False,
        ),
    }
    if state.get("app_id") and not new_app:
        request["app_id"] = state["app_id"]
    imported = console.call("/apps/imports", request)
    if imported.get("status") not in (
        "completed",
        "completed-with-warnings",
        "completed_with_warnings",
    ):
        raise RuntimeError(f"DSL import did not complete: {imported.get('status')}")
    app_id = imported["app_id"]
    draft = console.call(f"/apps/{app_id}/workflows/draft")
    node_count = len(draft["graph"]["nodes"])
    if node_count != len(dsl["workflow"]["graph"]["nodes"]):
        raise RuntimeError("Imported draft node count differs from DSL")
    if new_app:
        state.pop("api_key", None)
        state.pop("play_url", None)
    state.update(
        {
            "app_id": app_id,
            "base_url": "http://localhost:18080",
            "dsl_path": dsl_path.name,
        }
    )
    state_file.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    state_file.chmod(0o600)

    evidence = {
        "kind": "real_dify_import",
        "created_at": datetime.now(UTC).isoformat(),
        "dify_version": "1.17.1",
        "dsl_version": dsl["version"],
        "app_id": app_id,
        "import_status": imported["status"],
        "node_count": node_count,
        "clean_new_app": new_app,
        "model_executed": False,
        "credentials_included": False,
    }
    evidence_path = project / "evidence" / evidence_name
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return state


def publish(console: Console, project: Path, state: dict[str, Any]) -> dict[str, Any]:
    app_id = state["app_id"]
    console.call(
        f"/apps/{app_id}/workflows/publish",
        {
            "marked_name": "比赛体验",
            "marked_comment": "记忆失物招领处可复现 E2E",
        },
    )
    if not state.get("api_key"):
        token = console.call(f"/apps/{app_id}/api-keys", {})
        state["api_key"] = token["token"]
    app = console.call(f"/apps/{app_id}")
    site = app.get("site") or {}
    code = site.get("access_token") or site.get("code")
    state["play_url"] = state["base_url"] + "/chat/" + code if code else ""
    state["api_base_url"] = state["base_url"] + "/v1"
    evidence = {
        "kind": "real_dify_local_publish",
        "created_at": datetime.now(UTC).isoformat(),
        "app_id": app_id,
        "provider": PROVIDER,
        "plugin": PLUGIN,
        "model": FIXTURE_MODEL,
        "model_execution_mode": "deterministic_fixture",
        "published": True,
        "credentials_included": False,
    }
    evidence_path = project / "evidence" / "dify-publish.json"
    evidence_path.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return state


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--base-url", default="http://localhost:18080")
    parser.add_argument("--dsl", type=Path)
    parser.add_argument("--clean-reimport", action="store_true")
    args = parser.parse_args()

    root = args.root.resolve()
    project = Path(__file__).resolve().parents[1]
    dsl_path = (args.dsl or project / "dsl" / "memory-museum.e2e.yml").resolve()
    wait_http(args.base_url + "/console/api/setup")
    console = Console(args.base_url)
    login_local(console, root)

    console.call(
        "/workspaces/current/plugin/install/marketplace",
        {"plugin_unique_identifiers": [PLUGIN]},
    )
    wait_provider(console)
    configure_fixture_model(console)

    state = import_app(
        console=console,
        root=root,
        project=project,
        dsl_path=dsl_path,
        new_app=False,
        evidence_name="dify-import.json",
    )
    if args.clean_reimport:
        state = import_app(
            console=console,
            root=root,
            project=project,
            dsl_path=dsl_path,
            new_app=True,
            evidence_name="dify-reimport.json",
        )
    state = publish(console, project, state)
    state_file = root / "dify-local.json"
    state_file.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    state_file.chmod(0o600)
    print(
        json.dumps(
            {
                "result": "imported_and_published",
                "app_id": state["app_id"],
                "play_url": state.get("play_url"),
                "model_execution_mode": "deterministic_fixture",
                "credentials_printed": False,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
