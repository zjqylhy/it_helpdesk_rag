from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Login(Input):
    username: str = Field(min_length=1, max_length=100)
    # 密码不裁剪，兼容已有 bcrypt 账号。
    model_config = ConfigDict(extra="forbid")
    password: str = Field(min_length=1, max_length=200)


class UserCreate(Login):
    role: Literal["admin", "user"] = "user"
    usage_kind: Literal["demo", "real_usage"] = "demo"


class KnowledgeBase(Input):
    name: str = Field(min_length=1, max_length=200)
    organization: str = Field(min_length=1, max_length=200)


class Enabled(Input):
    enabled: bool = Field(strict=True)


class Disabled(Input):
    disabled: bool = Field(strict=True)


class UserUpdate(Input):
    model_config = ConfigDict(extra="forbid")
    disabled: bool | None = Field(default=None, strict=True)
    usage_kind: Literal["demo", "real_usage"] | None = None
    password: str | None = Field(default=None, min_length=12, max_length=72)


class Group(Input):
    name: str = Field(min_length=1, max_length=200)


class Members(Input):
    user_ids: list[str] = Field(max_length=1000)


class DocumentAccess(Input):
    visibility: Literal["all_users", "groups"]
    group_ids: list[str] = Field(default_factory=list, max_length=100)


class SessionCreate(Input):
    kb_ids: list[str] = Field(default_factory=list, max_length=32)
    kb_id: str | None = None
