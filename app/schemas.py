from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Submit(Input):
    script: str = Field(min_length=1, max_length=100000)


class Gate(Input):
    gate: Literal['img', 'aud']
    passed: bool
    revision: int = Field(ge=0)


class Confirm(Input):
    confirmed: bool = False


class RegenImage(Confirm):
    expected_revision: int = Field(ge=0)
    idempotency_key: str = Field(min_length=16, max_length=128, pattern=r'^[A-Za-z0-9_.:-]+$')


class Generate(Confirm):
    revision: int = Field(ge=1)


class Plan(Input):
    scenes: list[str] = Field(min_length=1, max_length=100)
    revision: int = Field(ge=1)


class ConfigEdit(Input):
    text: str = Field(max_length=32768)
    revision: str = Field(min_length=64, max_length=64)
    confirm_binding: bool = False


class ConfigRestore(Input):
    revision: str = Field(min_length=64, max_length=64)
    confirm_binding: bool = False


class Login(Input):
    token: str = Field(min_length=1, max_length=4096)


class Failure(BaseModel):
    code: str
    message: str
    fields: list[str] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    error: Failure
    detail: str
    request_id: str


class Ok(BaseModel):
    ok: bool = True
    message: str | None = None


class SessionInfo(BaseModel):
    admin: bool
    ephemeral: bool
    api_version: str = 'v1'
    visitor_expires_in: int
    limits: dict[str, int]


class Health(BaseModel):
    status: str = 'ok'
    api_version: str = 'v1'


class Accepted(BaseModel):
    task_id: int
    status_url: str
    accepted: bool = True
    needs_confirm: bool = False
    message: str | None = None
    operation_id: str | None = None
    replayed: bool = False


class TaskSummary(BaseModel):
    id: int
    status: str
    archived: int
    created_at: str | None
    scenes: int
    preview: str


class TaskList(BaseModel):
    tasks: list[TaskSummary]


class Scene(BaseModel):
    idx: int
    narration: str
    img_ok: int
    aud_ok: int
    img_placeholder: int
    img_state: str
    aud_state: str
    img_revision: int
    aud_revision: int
    img_approved: int
    aud_approved: int
    img_error: str | None = None
    aud_error: str | None = None
    img_busy: bool
    aud_busy: bool
    image: str | None = None
    audio: str | None = None


class TaskDetail(BaseModel):
    id: int
    script: str
    status: str
    error: str | None = None
    created_at: str | None = None
    plan_complete: int
    expected_scenes: int
    plan_revision: int
    image_requests: int
    archived: int
    busy: bool
    configured: bool
    model_available: dict[str, bool]
    image_request_limit: int
    config_revision: str
    video: str | None = None
    scenes: list[Scene]


class ModelSettings(BaseModel):
    text: str
    revision: str
    secrets: dict[str, bool]
    has_previous: bool
    ephemeral: bool
    storage: str
    external_change: bool
