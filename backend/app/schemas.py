import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)


def _check_password_bytes(v: str) -> str:
    # bcrypt silently ignores everything past 72 bytes; reject instead of truncating.
    if len(v.encode("utf-8")) > 72:
        raise ValueError("Password must be at most 72 bytes")
    return v


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)
    name: str = Field(min_length=1, max_length=80)

    _pw = field_validator("password")(_check_password_bytes)

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Name must not be blank")
        return v


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class AuthResponse(BaseModel):
    token: str
    id: uuid.UUID
    email: str
    name: str


class MeResponse(BaseModel):
    id: uuid.UUID
    email: str
    name: str


class CreateConversationRequest(BaseModel):
    title: str | None = Field(default=None, max_length=200)


class RenameConversationRequest(BaseModel):
    title: str | None = Field(default=None, max_length=200)


class ConversationDto(CamelModel):
    id: uuid.UUID
    title: str
    created_at: datetime
    updated_at: datetime


class MessageDto(CamelModel):
    id: uuid.UUID
    role: str
    content: str | None
    blocks: list[Any] | None
    sources: list[str] | None
    follow_ups: list[Any] | None
    created_at: datetime


class QueryRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
