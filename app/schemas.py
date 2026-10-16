from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Submit(Input):
    script: str = Field(min_length=1, max_length=100000)
    aspect_ratio: Literal['16:9', '9:16'] = '16:9'
    image_fit: Literal['contain', 'cover'] = 'contain'
    purpose: str = Field(default='', max_length=80)
    audience: str = Field(default='', max_length=80)
    focus: str = Field(default='', max_length=160)
    target_duration_seconds: int | None = None
    title: str = Field(default='', max_length=80)


class Rename(Input):
    title: str = Field(default='', max_length=80)


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
    preview: bool = False


class PlanScene(Input):
    id: int | None = Field(default=None, ge=1)
    narration: str = Field(min_length=1, max_length=100000)
    visual_prompt: str = Field(min_length=1, max_length=4000)
    image: 'ImageSpec | None' = None


class ImageSpec(Input):
    source: Literal['generated', 'asset']
    asset_id: int | None = Field(default=None, ge=1)
    fixed_reference_asset_ids: list[int] = Field(default_factory=list, max_length=16)
    previous_scene_id: int | None = Field(default=None, ge=1)

    @model_validator(mode='after')
    def asset_is_required(self):
        if self.source == 'asset' and self.asset_id is None:
            raise ValueError('使用已有图片时必须选择图片')
        if self.source == 'asset' and self.fixed_reference_asset_ids:
            raise ValueError('直接使用已有图片时不能配置生成参考')
        if len(set(self.fixed_reference_asset_ids)) != len(self.fixed_reference_asset_ids):
            raise ValueError('固定参考图不能重复')
        if self.source == 'asset' and self.previous_scene_id is not None:
            raise ValueError('直接使用已有图片时不能引用前镜')
        return self


class Plan(Input):
    scenes: list[str | PlanScene] = Field(min_length=1, max_length=100)
    revision: int = Field(ge=1)
    aspect_ratio: Literal['16:9', '9:16'] | None = None
    image_fit: Literal['contain', 'cover'] | None = None
    target_duration_seconds: int | None = None


class PlanImpactScene(BaseModel):
    id: int | None = None
    index: int
    image: Literal['keep', 'redo', 'select']
    audio: Literal['keep', 'redo']
    img_approval_kept: bool
    aud_approval_kept: bool
    invalidated_by_upstream: bool = False


class PlanImpact(BaseModel):
    ok: bool = True
    plan_revision: int
    image_requests_needed: int
    scenes: list[PlanImpactScene]
    removed_scene_ids: list[int] = Field(default_factory=list)


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


class VideoVersion(BaseModel):
    id: int
    label: str
    created_at: str
    video: str


class TaskSummary(BaseModel):
    id: int
    status: str
    archived: int
    created_at: str | None
    scenes: int
    preview: str
    title: str = ''


class TaskList(BaseModel):
    tasks: list[TaskSummary]


class ImageAsset(BaseModel):
    id: int
    source: Literal['upload', 'generated']
    url: str
    sha256: str
    mime: str
    width: int
    height: int
    byte_size: int
    origin_scene_id: int | None = None
    context: dict = Field(default_factory=dict)
    created_at: str


class Scene(BaseModel):
    id: int
    idx: int
    narration: str
    visual_prompt: str
    image_source: Literal['generated', 'asset'] = 'generated'
    image_asset_id: int | None = None
    fixed_reference_asset_ids: list[int] = Field(default_factory=list)
    previous_scene_id: int | None = None
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
    audio_duration_seconds: float | None = None
    image_width: int | None = None
    image_height: int | None = None


class Progress(BaseModel):
    stage: str | None = None
    scene_index: int | None = None
    elapsed_seconds: int = 0
    operation_elapsed_seconds: int = 0
    images_ready: int
    audio_ready: int
    total: int


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
    missing_images: int
    image_requests_needed: int
    image_capabilities: dict[str, object]
    audio_duration_seconds: float | None = None
    max_video_seconds: int
    aspect_ratio: Literal['16:9', '9:16']
    image_fit: Literal['contain', 'cover']
    purpose: str = ''
    audience: str = ''
    focus: str = ''
    target_duration_seconds: int = 0
    narration_budget_chars: int | None = None
    narration_chars: int = 0
    narration_chars_per_second: int = 4
    pause_requested: int = 0
    title: str = ''
    copied_from: int | None = None
    versions: list[VideoVersion] = Field(default_factory=list)
    image_assets: list[ImageAsset] = Field(default_factory=list)
    progress: Progress
    video: str | None = None
    scenes: list[Scene]


class ModelSettings(BaseModel):
    text: str
    document: dict
    revision: str
    secrets: dict[str, bool]
    has_previous: bool
    ephemeral: bool
    storage: str
    external_change: bool
    complete_config: bool = False
    restart_required: list[str] = Field(default_factory=list)


class ConfigValidation(Ok):
    document: dict
