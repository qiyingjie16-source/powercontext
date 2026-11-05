"""Start, stop, inspect, or lock only this submission's Compose projects."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path


def capture(*args: str) -> str:
    return subprocess.check_output(
        args,
        text=True,
        encoding="utf-8",
    ).strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("up", "down", "status", "lock-images"))
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()

    project = Path(__file__).resolve().parents[1]
    root = args.root.resolve()
    desktop_plugins = Path("C:/Program Files/Docker/Docker/resources/cli-plugins")
    if os.name == "nt" and desktop_plugins.is_dir():
        client = root / "docker-client"
        client.mkdir(parents=True, exist_ok=True)
        config = client / "config.json"
        if not config.exists():
            config.write_text(
                json.dumps({"cliPluginsExtraDirs": [str(desktop_plugins)]}),
                encoding="utf-8",
            )
        os.environ["DOCKER_CONFIG"] = str(client)
        os.environ["DOCKER_HOST"] = "npipe:////./pipe/dockerDesktopLinuxEngine"

    runtime = root / "dify-runtime" / "docker"
    if not (runtime / ".env").exists() or not (project / "deploy" / ".env").exists():
        raise SystemExit("Run bootstrap.py --root <directory> first.")

    dify = [
        "docker",
        "compose",
        "-p",
        "memory-museum-dify",
        "--project-directory",
        str(runtime),
        "--env-file",
        str(runtime / ".env"),
        "-f",
        str(runtime / "docker-compose.yaml"),
        "-f",
        str(project / "deploy" / "dify.override.yaml"),
    ]
    lockfile = project / "deploy" / "dify-images.lock.json"
    if lockfile.exists():
        dify += ["-f", str(lockfile)]
    museum = [
        "docker",
        "compose",
        "-p",
        "memory-museum",
        "--env-file",
        str(project / "deploy" / ".env"),
        "-f",
        str(project / "deploy" / "compose.yaml"),
    ]

    if args.command == "up":
        existing = subprocess.run(
            ["docker", "network", "inspect", "museum-shared"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if existing.returncode:
            subprocess.run(
                [
                    "docker",
                    "network",
                    "create",
                    "--driver",
                    "bridge",
                    "--subnet",
                    "172.31.92.0/24",
                    "museum-shared",
                ],
                check=True,
            )
        else:
            network = json.loads(
                capture("docker", "network", "inspect", "museum-shared")
            )[0]
            if network["IPAM"]["Config"][0]["Subnet"] != "172.31.92.0/24":
                raise SystemExit(
                    "Existing museum-shared uses another subnet; refusing to modify it."
                )
        subprocess.run(dify + ["up", "-d", "--quiet-pull"], check=True)
        subprocess.run(museum + ["up", "-d", "--build"], check=True)
        print(
            "Dify: http://localhost:18080 | "
            "Museum health: http://localhost:18081/health | "
            "PowerContext: http://localhost:18082"
        )
    elif args.command == "down":
        subprocess.run(museum + ["down"], check=True)
        subprocess.run(dify + ["down"], check=True)
        print("Stopped this project. Data and the shared network were retained.")
    elif args.command == "status":
        subprocess.run(dify + ["ps"], check=True)
        subprocess.run(museum + ["ps"], check=True)
    else:
        config = json.loads(capture(*dify, "config", "--format", "json"))
        services = {}
        for name, service in config["services"].items():
            image = service.get("image")
            if image:
                info = json.loads(capture("docker", "image", "inspect", image))[0]
                digests = info.get("RepoDigests") or []
                if not digests:
                    raise SystemExit(f"No registry digest for {name}; pull the image first.")
                services[name] = {"image": digests[0]}
        lockfile.write_text(
            json.dumps({"services": services}, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"Locked {len(services)} Dify service images: {lockfile}")


if __name__ == "__main__":
    main()
