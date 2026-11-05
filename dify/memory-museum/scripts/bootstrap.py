"""Fetch pinned upstream sources and create local-only Compose secrets."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
from pathlib import Path


DIFY_COMMIT = "8387590ace4a094de812b7847fc6a4c3a27cd52b"
POWERCONTEXT_COMMIT = "729dc4c5548f915785a06c318f8cb7ed6cbea45b"


def run(*args: str, cwd: Path | None = None) -> str:
    return subprocess.check_output(
        args,
        cwd=cwd,
        text=True,
        encoding="utf-8",
    ).strip()


def checkout(path: Path, repository: str, commit: str) -> None:
    if not (path / ".git").exists():
        run("git", "clone", "--filter=blob:none", repository, str(path))
    if run("git", "status", "--porcelain", cwd=path):
        raise SystemExit(f"Upstream checkout has local changes; refusing to overwrite: {path}")
    try:
        run("git", "cat-file", "-e", commit, cwd=path)
    except subprocess.CalledProcessError:
        run("git", "fetch", "--depth", "1", "origin", commit, cwd=path)
    run("git", "checkout", "--detach", commit, cwd=path)


def write_new(path: Path, content: str, *, mode: int = 0o600) -> bool:
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    try:
        path.chmod(mode)
    except OSError:
        pass
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()

    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    project = Path(__file__).resolve().parents[1]
    checkout(
        root / "dify-runtime",
        "https://github.com/langgenius/dify.git",
        DIFY_COMMIT,
    )
    checkout(
        root / "powercontext-source",
        "https://github.com/oceanbase/powercontext.git",
        POWERCONTEXT_COMMIT,
    )
    data = root / "data"
    for name in ("museum", "powercontext"):
        path = data / name
        path.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            # Both containers run as UID 10001. The dedicated local test data
            # directory must be writable without requiring root on Linux CI.
            path.chmod(0o777)

    museum_key = secrets.token_urlsafe(32)
    powercontext_token = secrets.token_urlsafe(32)
    deploy_env = "\n".join(
        [
            "MUSEUM_SERVICE_KEY=" + museum_key,
            "POWERCONTEXT_TOKEN=" + powercontext_token,
            f"POWERCONTEXT_SOURCE={(root / 'powercontext-source').as_posix()}",
            "POWERCONTEXT_VERSION=1.5.0",
            f"MUSEUM_DATA={data.as_posix()}",
            "MUSEUM_PORT=18081",
            "POWERCONTEXT_PORT=18082",
            "",
        ]
    )
    deploy_file = project / "deploy" / ".env"
    if not write_new(deploy_file, deploy_env):
        existing = {
            line.split("=", 1)[0]: line.split("=", 1)[1]
            for line in deploy_file.read_text(encoding="utf-8").splitlines()
            if "=" in line and not line.lstrip().startswith("#")
        }
        museum_key = existing.get("MUSEUM_SERVICE_KEY", museum_key)
        powercontext_token = existing.get("POWERCONTEXT_TOKEN", powercontext_token)

    upstream = root / "dify-runtime" / "docker"
    base_env = (upstream / ".env.example").read_text(encoding="utf-8")
    overrides = {
        "EXPOSE_NGINX_PORT": "18080",
        "EXPOSE_NGINX_SSL_PORT": "18443",
        "EXPOSE_PLUGIN_DEBUGGING_PORT": "15003",
        "NEXT_PUBLIC_SOCKET_URL": "ws://localhost:18080",
        "SECRET_KEY": secrets.token_urlsafe(48),
        "DB_PASSWORD": secrets.token_urlsafe(32),
        "REDIS_PASSWORD": secrets.token_urlsafe(32),
        "PLUGIN_DAEMON_KEY": secrets.token_urlsafe(32),
        "PLUGIN_DIFY_INNER_API_KEY": secrets.token_urlsafe(32),
        "DIFY_AGENT_API_TOKEN": secrets.token_urlsafe(32),
        "DIFY_AGENT_SERVER_SECRET_KEY": secrets.token_urlsafe(32),
        "DIFY_AGENT_LOCAL_SANDBOX_AUTH_TOKEN": secrets.token_urlsafe(32),
        "CODE_EXECUTION_API_KEY": secrets.token_urlsafe(32),
        "SSRF_PROXY_ALLOW_PRIVATE_IPS": "172.31.92.0/24",
        "COMPOSE_PROFILES": "postgresql,collaboration",
        "NEXT_TELEMETRY_DISABLED": "1",
    }
    overrides["SANDBOX_API_KEY"] = overrides["CODE_EXECUTION_API_KEY"]
    overrides["CELERY_BROKER_URL"] = (
        f"redis://:{overrides['REDIS_PASSWORD']}@redis:6379/1"
    )
    lines: list[str] = []
    for line in base_env.splitlines():
        key = line.split("=", 1)[0]
        lines.append(f"{key}={overrides.pop(key)}" if key in overrides else line)
    lines.extend(f"{key}={value}" for key, value in overrides.items())
    write_new(upstream / ".env", "\n".join(lines) + "\n")

    print(
        json.dumps(
            {
                "root": str(root),
                "dify": "http://localhost:18080",
                "museum": "http://localhost:18081",
                "powercontext": "http://localhost:18082",
                "secrets_file": str(deploy_file),
                "credentials_included": False,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
