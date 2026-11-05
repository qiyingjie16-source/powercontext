from __future__ import annotations

import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import configure_dify  # noqa: E402


class FakeConsole:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def call(self, path, payload=None, method=None):
        self.calls.append((path, payload, method))
        return self.responses.pop(0)


def test_install_plugin_polls_install_task():
    console = FakeConsole(
        [
            {"all_installed": False, "task_id": "task-1"},
            {
                "task": {
                    "status": "running",
                    "completed_plugins": 0,
                    "total_plugins": 1,
                }
            },
            {
                "task": {
                    "status": "success",
                    "completed_plugins": 1,
                    "total_plugins": 1,
                    "plugins": [],
                }
            },
        ]
    )

    result = configure_dify.install_plugin(console)

    assert result["result"] == "installed"
    assert [entry["status"] for entry in result["status_history"]] == [
        "running",
        "success",
    ]
    assert console.calls[-1][0] == "/workspaces/current/plugin/tasks/task-1"


def test_install_plugin_surfaces_task_failure():
    console = FakeConsole(
        [
            {"all_installed": False, "task_id": "task-2"},
            {
                "task": {
                    "status": "failed",
                    "completed_plugins": 0,
                    "total_plugins": 1,
                    "plugins": [
                        {
                            "plugin_id": "langgenius/openai_api_compatible",
                            "message": "download failed",
                        }
                    ],
                }
            },
        ]
    )

    with pytest.raises(RuntimeError, match="download failed"):
        configure_dify.install_plugin(console)


def test_install_plugin_accepts_already_installed_response():
    console = FakeConsole([{"all_installed": True}])

    result = configure_dify.install_plugin(console)

    assert result["result"] == "already_installed"
    assert len(console.calls) == 1


def test_fixture_credential_name_fits_dify_limit():
    console = FakeConsole([{"result": "success"}])

    configure_dify.configure_fixture_model(console)

    _, payload, _ = console.calls[0]
    assert len(payload["name"]) <= 30
    assert payload["model_type"] == "llm"
    assert payload["credentials"]["endpoint_url"].endswith("/model/v1")
