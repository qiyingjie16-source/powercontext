from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints


Identity = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=3,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:@-]+$",
    ),
]


class PrepareRequest(BaseModel):
    user_id: Identity
    conversation_id: str = Field(default="", max_length=256)
    turn_id: str = Field(default="", max_length=256)
    query: str = Field(min_length=1, max_length=2000)
    max_bytes: int = Field(default=8000, ge=512, le=32768)


class RememberRequest(BaseModel):
    user_id: Identity
    turn_id: str = Field(min_length=1, max_length=256)
    kind: str = Field(default="memory_museum_exhibit", min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=8192)
    description: str | None = Field(default=None, max_length=200)
    reason: str | None = Field(default=None, max_length=512)
