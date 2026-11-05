from __future__ import annotations

import hmac
import os
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException

from .model_fixture import router as model_router
from .models import PrepareRequest, RememberRequest
from .powercontext import PowerContextClient, PowerContextError
from .safety import sensitive_reason
from .store import ScopeStore


def create_app(
    *,
    db_path: str | None = None,
    service_key: str | None = None,
    powercontext_url: str | None = None,
    powercontext_token: str | None = None,
    powercontext_client: PowerContextClient | None = None,
):
    key = service_key if service_key is not None else os.environ.get("MUSEUM_SERVICE_KEY", "")
    if not key:
        raise RuntimeError("MUSEUM_SERVICE_KEY must be configured")

    store = ScopeStore(db_path or os.environ.get("MUSEUM_DB_PATH", "/data/museum.sqlite3"))
    if powercontext_client is None:
        token = (
            powercontext_token
            if powercontext_token is not None
            else os.environ.get("POWERCONTEXT_TOKEN", "")
        )
        if not token:
            raise RuntimeError("POWERCONTEXT_TOKEN must be configured")
        powercontext_client = PowerContextClient(
            powercontext_url or os.environ.get("POWERCONTEXT_URL", "http://powercontext:8000"),
            token,
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        powercontext_client.close()

    app = FastAPI(
        title="Memory Museum BFF",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    def authenticate(
        x_museum_key: Annotated[str | None, Header(alias="X-Museum-Key")] = None,
    ) -> None:
        if x_museum_key is None or not hmac.compare_digest(
            x_museum_key.encode(),
            key.encode(),
        ):
            raise HTTPException(status_code=401, detail="invalid museum service key")

    dependencies = [Depends(authenticate)]

    def scope_for(user_id: str) -> str:
        existing = store.get_scope(user_id)
        if existing:
            return existing
        created = powercontext_client.create_scope(user_id)
        store.put_scope(user_id, created)
        return created

    @app.get("/health")
    def health() -> dict[str, object]:
        with store.connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return {
            "ok": True,
            "service": "memory-museum",
            "storage": "sqlite",
            "powercontext": "configured",
            "model_fixture": "museum-fixture-v1",
        }

    @app.post("/v1/prepare", dependencies=dependencies)
    def prepare(request: PrepareRequest) -> dict[str, object]:
        reason = sensitive_reason(request.query)
        if reason:
            return {
                "ok": False,
                "status": "blocked",
                "memory_context": "",
                "content_bytes": 0,
                "memory_refs": [],
                "scope_id": None,
                "error": {"code": "sensitive_content", "message": reason, "uncertain": False},
            }
        try:
            scope_id = scope_for(request.user_id)
            prepared = powercontext_client.prepare(scope_id, request.query, request.max_bytes)
            return {
                "ok": True,
                "status": prepared.status,
                "memory_context": prepared.content,
                "content_bytes": prepared.content_bytes,
                "memory_refs": prepared.refs,
                "scope_id": scope_id,
            }
        except PowerContextError as error:
            return {
                "ok": False,
                "status": "unavailable",
                "memory_context": "",
                "content_bytes": 0,
                "memory_refs": [],
                "scope_id": None,
                "error": {
                    "code": error.code,
                    "message": str(error),
                    "uncertain": error.uncertain,
                },
            }

    @app.post("/v1/remember", dependencies=dependencies)
    def remember(request: RememberRequest) -> dict[str, object]:
        reason = sensitive_reason(
            " ".join(
                part
                for part in (request.text, request.description, request.reason)
                if part
            )
        )
        if reason:
            return {
                "ok": False,
                "status": "blocked",
                "changed": False,
                "records": [],
                "scope_id": None,
                "error": {"code": "sensitive_content", "message": reason, "uncertain": False},
            }
        try:
            scope_id = scope_for(request.user_id)
            source = powercontext_client.capture_source(
                scope_id,
                request.turn_id,
                request.text,
            )
            saved = powercontext_client.remember(
                scope_id,
                kind=request.kind,
                text=request.text,
                description=request.description,
                reason=request.reason,
                source=source,
            )
            return {
                "ok": True,
                "status": "success",
                "changed": saved.get("changed"),
                "records": saved.get("records") or [],
                "source": source.get("source"),
                "source_position": source.get("position"),
                "scope_id": scope_id,
            }
        except PowerContextError as error:
            return {
                "ok": False,
                "status": "unknown" if error.uncertain else "error",
                "changed": False,
                "records": [],
                "scope_id": None,
                "error": {
                    "code": error.code,
                    "message": str(error),
                    "uncertain": error.uncertain,
                },
            }

    app.include_router(model_router)
    return app
