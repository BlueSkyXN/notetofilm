# NoteToFilm API v1

All business functions use `/api/v1`. HTML/CSS/JavaScript are static clients: no server-injected task IDs, configuration or credentials. FastAPI can serve the bundled frontend, or a reverse proxy can serve these files separately while routing `/api/v1/*` to the backend under the same origin. There is no requirement to open a page to use the API.

- Read-only API explorer: `/api-docs` (no external CDN, no automatic inference).
- Machine-readable request/response schemas: `/api/v1/openapi.json`.
- Health: `GET /api/v1/health`; `/healthz` remains the container health-check alias.
- Pre-v1 `/api/*` and `/files/*` routes are removed. Frontend and backend must be upgraded together.
- This remains one process and one data volume, not a distributed or multi-worker service.

## Authentication and isolation

Anonymous users do not log in. A client first calls `GET /api/v1/session`, then preserves `ntf_visitor` in its cookie jar. The signed HttpOnly cookie identifies the visitor and expires after 30 days. Clearing it or switching devices loses access to the visitor's existing tasks. All task metadata, writes and media downloads enforce ownership; another visitor receives 404. An HTML shell being public does not grant access to its task data.

Administrators call `POST /api/v1/auth/login` with `{"token":"<administrator token>"}` and retain `ntf_admin`, or send `X-Access-Token`. Obtain this token from the private full configuration; do not put it in a URL, checked-in client, example file or command history. `POST /api/v1/auth/logout` clears the administrator browser cookie. It does not revoke previously copied cookies or the header token; rotating the deployment token invalidates both admin and visitor signatures.

Cookie-bearing writes are same-origin and reject cross-site browser requests. CORS is deliberately not enabled. To deploy frontend and backend separately, keep them behind the same public origin. A third-party backend/CLI can use HTTPS and a cookie jar without rendering HTML. Do not disable CSRF protections or expose administrator tokens in frontend JavaScript to support arbitrary browser origins.

## Main endpoints

| Method | Path after `/api/v1` | Purpose |
|---|---|---|
| GET | `/session` | Establish visitor cookie; return role, storage warning and selected limits |
| POST | `/auth/login` | Administrator login |
| POST | `/auth/logout` | Clear administrator cookie |
| GET | `/tasks` | Own tasks; administrators see all |
| POST | `/tasks` | Submit script; 202 with task ID and polling URL |
| GET | `/tasks/{id}` | Current status, plan, material revisions and media URLs |
| GET | `/tasks/{id}/images` | List registered images available to this task |
| POST | `/tasks/{id}/images` | Upload raw PNG/JPEG bytes for this task |
| PUT | `/tasks/{id}/plan` | Edit plan with expected revision |
| POST | `/tasks/{id}/plan/impact` | Read-only preview of what a plan edit would redo; no writes or model calls |
| POST | `/tasks/{id}/generate` | Explicitly confirm generation of current plan revision |
| PUT | `/tasks/{id}/scenes/{idx}/gate` | Approve/revoke current image or audio revision |
| POST | `/tasks/{id}/scenes/{idx}/regen-image` | Explicitly confirmed image regeneration |
| POST | `/tasks/{id}/scenes/{idx}/regen-audio` | Audio regeneration |
| POST | `/tasks/{id}/retry` | Recovery; JSON `confirmed`, never a query-string payment switch |
| POST | `/tasks/{id}/render` | Compose approved materials |
| POST | `/tasks/{id}/archive` | Archive without deleting files |
| GET | `/tasks/{id}/files/{name}` | Authorized inline media for image, audio and video playback |
| GET | `/tasks/{id}/download` | Current completed video as an MP4 attachment; same ownership checks |
| GET | `/admin/config` | Administrator-only TOML and configured-secret flags |
| POST | `/admin/config/validate` | Validate complete draft; no model calls |
| PUT | `/admin/config` | Save and apply with expected revision and binding confirmation |
| POST | `/admin/config/restore` | Restore previous TOML with expected revision |
| GET | `/admin/config/export` | Download masked TOML; explicit `?include_secrets=true` exports the complete private file |

## Async contract

Generation endpoints return HTTP 202 immediately after admission:

```json
{
  "task_id": 1,
  "status_url": "/api/v1/tasks/1",
  "accepted": true,
  "needs_confirm": false,
  "message": null
}
```

`POST /tasks` also returns a `Location` header. Poll the returned URL rather than resubmitting. Status transitions include `generating`, `planned`, `review`, `rendering`, `done`, `failed`, `interrupted`. A 202 means accepted, not completed and not paid-provider success.

`POST /tasks/{id}/retry` with `{"confirmed":false}` may return 200 with `accepted:false` and `needs_confirm:true`; ask the user before repeating with `confirmed:true`. A timeout can mean an upstream call was billed. There are no automatic retries, and clients must not automatically retry mutation calls. Server-side ownership, revisions, active-operation checks and duplicate-script protection remain in force; only paid image regeneration has persistent request-key replay protection; other mutation endpoints do not implement universal idempotency.

## Image regeneration replay protection

`POST /tasks/{id}/scenes/{idx}/regen-image` requires:

```json
{"confirmed":true,"expected_revision":1,"idempotency_key":"a-new-uuid-for-this-intent"}
```

Use the current `img_revision`. Keep the same key and parameters when the outcome of submission is unknown; do not create a new key for a network retry. Keys are scoped to the authenticated requester and persist in SQLite. Same key/same parameters returns the original `operation_id` with `replayed:true`, including after completion, failure or restart. Replay does not dispatch work or consume another quota. Same key/different parameters and new intents with stale revision return 409. A 202 replay means the original request was admitted, not that it succeeded; poll `status_url`. An interrupted or failed operation still requires explicit recovery confirmation when new paid calls are needed.

Old image-regeneration clients without the required fields receive 422. Deploy frontend/backend together. Audio regeneration keeps its existing contract.

## Request bodies and model availability

`POST /tasks` accepts `script` and optional `aspect_ratio` (`16:9` default, or `9:16`) and `image_fit` (`contain` default, or `cover`). Output dimensions are 1920×1080 or 1080×1920. `contain` preserves the whole image with white padding; `cover` crops centrally to fill the output. The configured image request size is unchanged; actual returned images may have a different size.

Optional planning fields `purpose`, `audience`, `focus` and `target_duration_seconds` must be sent together or omitted together. They change only the storyboard prompt and the narration budget (`target_duration_seconds * 4` characters, capped by the script limit). They do not change image requests: visuals still must not contain text, numbers, buttons or charts. Duplicate protection includes these fields, so the same script with a different brief can be submitted. Task details return the brief, `narration_budget_chars` (null when unset), `narration_chars` and `narration_chars_per_second`. The character rate is a planning estimate; actual narration length is `audio_duration_seconds` after audio is ready. `POST /tasks/{id}/generate` returns 409 when the narration exceeds that budget, before any image or audio request. Pass `"preview": true` to generate only scene 0 and return to `planned`; omit it or pass false to continue the whole plan. `POST /tasks/{id}/pause` during generation finishes the current provider request, keeps finished assets, and leaves the task `interrupted` so retry can continue. Optional `title` names a task. `POST /tasks/{id}/revise` keeps the current MP4 as a downloadable version and returns the task to `review`. `POST /tasks/{id}/copy` creates a new planned task with the same original script, narrations, visual prompts and brief. The new draft has new scene IDs, defaults to generated images and clears all image assets and reference bindings; select images/references again before generation. `PUT /tasks/{id}/plan` may include `target_duration_seconds` to raise or lower the budget without regenerating scenes; omit it to leave the saved target unchanged.

`PUT /tasks/{id}/plan` accepts the full scene list and current `plan_revision` while an unarchived task is idle in `planned`, `review`, `failed` or `interrupted` and has a complete plan:

```json
{"revision":2,"aspect_ratio":"9:16","image_fit":"cover","scenes":[{"id":12,"narration":"A short narration.","visual_prompt":"An orange cat sitting by a sunlit window.","image":{"source":"asset","asset_id":3,"fixed_reference_asset_ids":[],"previous_scene_id":null}}]}
```

Use each scene's returned stable `id` when editing or reordering; omit `id` for a new scene. Narration changes invalidate only audio; visual-prompt changes invalidate only generated images. Visual prompts must be nonempty and at most 4000 characters. Duplicate or stale IDs are rejected atomically. Legacy string arrays remain accepted: each string supplies both narration and visual prompt, with identical pairs matched one-to-one. Reordered scenes receive fresh material revisions so old index/revision approvals cannot target a different scene.

The optional `image` object selects `generated` or `asset`. `asset` requires an `asset_id` from the same task and publishes that immutable file as the scene image for review; it never calls the image provider. `generated` uses `asset_id:null` before generation and may list up to 16 `fixed_reference_asset_ids` from the same task plus one earlier stable `previous_scene_id`. The previous reference must remain before the dependent scene in the saved order; reordering that violates this returns 409. Omit `image` to preserve an existing scene's setting; new scenes default to generated without references. Uploads and newly generated images appear in `GET /tasks/{id}/images` and task details as `image_assets` with stable IDs, dimensions, hashes and authorized URLs. If a selected uploaded/reference file is missing, generation/recovery returns 409 rather than silently falling back to paid text-to-image.

Fixed and previous-scene references use the task's frozen image configuration. Set `image.reference_protocol="openai-images-edit"` for new tasks to enable the OpenAI Images Edit multipart adapter; existing snapshots default to `none`. A generated image records the prompt, model/size/quality summary, reference protocol, each fixed reference asset ID/hash, and the previous scene's actual asset ID/hash in `image_assets[].context`. Plain generated scenes continue to use `/images/generations` with the previous JSON contract. When an upstream image is regenerated, directly dependent scenes retain their old files but return to pending image state and lose approval; later transitive scenes are not changed until their direct upstream actually changes.

`POST /tasks/{id}/images` accepts the request body directly as PNG or JPEG bytes with the corresponding `Content-Type`; it does not use JSON or multipart. A first upload returns 201, an exact normalized duplicate returns 200 with the existing asset, and invalid type/content, byte overflow, or excessive decoded pixels returns 422/413 without publishing the asset or changing scenes.

Optional layout fields preserve their current values when omitted. A layout change retains image files, bumps image revisions and clears image approvals so users review the new framing without automatically requesting new images. Saving returns the task to `planned`; call `/generate` with its new revision to fill missing materials. Historical files and request counts remain. Busy and completed tasks cannot be edited.

`POST /tasks/{id}/plan/impact` previews the same edit before saving. It accepts the identical body as the plan save (scene list, `revision`, optional layout and duration fields) under the same ownership, task-status and revision checks — a stale revision returns 409 — and applies the same server-side impact rules without writing the database, triggering snapshots or calling providers. Each entry in the returned `scenes` carries the kept stable `id` (null for new scenes), `index`, `image` action (`keep`, `redo` or `select`), `audio` action (`keep` or `redo`), whether approvals survive (`img_approval_kept`, `aud_approval_kept`), and `invalidated_by_upstream` when a direct previous-scene reference will be invalidated because its upstream image is being redone. `removed_scene_ids` lists scenes dropped by the draft, and `image_requests_needed` counts the paid image requests the saved plan would require. The preview is an estimate for the current draft; saving re-validates everything.

Task details include `missing_images` (all unavailable images), `image_requests_needed` (only images still requiring text-to-image), `image_capabilities` (`reference_protocol`, `fixed_references_supported`, `max_fixed_references`), `audio_duration_seconds` (null until all audio durations are known and ready), and `max_video_seconds`; scenes expose their recorded `audio_duration_seconds`, `image_source`, `image_asset_id`, `fixed_reference_asset_ids`, and `previous_scene_id`. Overlong known totals return 409 from `/render` before reserving render quota. Media with unknown legacy durations is still checked before encoding. On a duration error, edit the plan rather than repeatedly requesting recovery/render.

Scenes also expose stable `id`, `visual_prompt` and `image_width`/`image_height` (null for legacy images until next validation). Task `progress` reports `stage`, zero-based `scene_index`, `elapsed_seconds` for the current step, `operation_elapsed_seconds`, `images_ready`, `audio_ready`, and total scene count. During concurrent generation `jobs` additionally reports the persisted in-flight list (`inflight` entries carry `scene_index` and `kind`) plus `done`/`total` counts for the current operation; `stage`/`scene_index` fall back to the first in-flight job by scene order for older clients. When idle, stage/index are null and elapsed values are zero. Counts track ready assets, not approval counts; placeholders do not count as ready images. No completion-time estimate is provided.

Administrator configuration accepts `[llm].wire_api="chat"` or `"responses"`. Missing values in existing TOML or task snapshots mean `chat`. The protocol is frozen with each task; a later configuration save affects new tasks only. Task snapshots retain credential references; in full-file mode these resolve against the current private configuration, without reading model environment variables. Responses requests use `/responses`, non-streaming output and `store=false`; incomplete/refused/missing text results are rejected without automatic fallback or retry.

Administrator configuration reads/saves return a parsed `document` for form editing. Full-file mode sets `complete_config:true` and returns `schema_version`, `server`, `storage`, `credentials`, `limits`, `llm`, `image`, and `tts`. Models use `credential_ref` (`shared`, `llm`, or `image`). Existing clients may still save the three legacy model sections with `api_key_env`; other sections are retained.

Normal reads, saves, validation and exports mask nonempty secret fields with `__UNCHANGED__`. Preserve that value to keep a secret, supply a new value to replace it, or an empty string to remove it. Only authenticated `GET /admin/config/export?include_secrets=true` returns the complete plaintext configuration. Responses use `Cache-Control: no-store`. A masked export is an editing document, not a complete disaster-recovery backup.

`/admin/config/validate` validates without saving or calling providers and returns a masked document. Clients must retain their original draft until saving; replacing it with the validation response would lose newly entered keys. Both editors use revision/binding checks. Models and credentials apply to subsequent use; task model parameters remain frozen. `restart_required` lists changed runtime settings requiring a restart. Storage locations must be migrated separately before switching; editing a bucket name does not copy data.

The application limits actual received bytes before business dispatch to `MAX_SCRIPT_CHARS * 6 + 196608` (316,608 bytes by default), including whitespace. Only `PUT /tasks/{id}/plan` and `POST /tasks/{id}/plan/impact` add `min(MAX_SCENES,100) * 4000 * 6` bytes for bounded visual prompts and JSON Unicode escapes, so any body that can be saved can also be previewed. `POST /tasks/{id}/images` instead uses `MAX_UPLOAD_IMAGE_BYTES` (10 MiB by default). Legal chunked JSON is allowed without Content-Length. Duplicate/conflicting framing headers or a declared/actual mismatch are rejected; actual overflow returns 413, total read timeout returns 408. JSON field length validation is an additional check, not a replacement. HTTP parsers/proxies may reject malformed traffic before the application; configure independent proxy limits and timeouts.

Task details expose only `model_available.llm`/`image` booleans. Removed provider keys do not block historical detail, downloads, local composition or completed-output recovery. Operations that require a missing key return 503 before that provider is called. Snapshot bindings are retained, never silently replaced with the current gateway.

## API-only example (no real credentials or external calls in this example)

```python
import http.cookiejar
import json
import urllib.request

base = "http://127.0.0.1:8765"
client = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
)

def api(path, body=None, method=None):
    req = urllib.request.Request(
        base + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method=method,
    )
    with client.open(req, timeout=30) as response:
        return json.load(response)

api("/api/v1/session")
# The following call can use the configured paid LLM. Invoke only with consent.
# task = api("/api/v1/tasks", {"script": "Your short script"})
# status = api(task["status_url"])
```

To generate: review the returned plan, then send `{"confirmed":true,"revision":<plan_revision>}` to `/generate`. Approve each material with `PUT .../gate`, body `{"gate":"img" or "aud","passed":true,"revision":<current revision>}`. Then POST `{}` to `/render` and poll to `done`. Use `video` for inline playback, or GET `/tasks/{id}/download` for the current published MP4 as an attachment (`notetofilm-{id}.mp4`), with the same client/cookie jar. Unfinished, missing or unowned videos return 404. Never guess internal media paths; use URLs returned by the API.

Task list items include `preview`: the first scene narration, or the source script before scenes exist, with newlines replaced by spaces and limited to 60 characters. It is plain text and should be escaped when displayed in HTML.

## Errors and privacy

All errors use the same JSON envelope, including middleware, validation and unexpected failures:

```json
{
  "error": {"code":"conflict", "message":"Human-readable explanation", "fields":[]},
  "detail":"Human-readable explanation",
  "request_id":"server-generated request ID"
}
```

`X-Request-ID` matches the body. Input validation reports field locations, not the original input values. Unexpected errors expose neither stack traces nor upstream responses. Known task errors retain safe, locally authored messages for HTTP status, network/TLS/timeouts, TTS and media limits. Upstream bodies, URLs and headers are never used as public error messages. Codes include `admin_required`, `forbidden`, `not_found`, `conflict`, `validation_error`, `quota_exceeded`, `unavailable`, `insufficient_storage`.

Response schemas whitelist public fields. Task details omit owner identifiers, raw configuration snapshots, database columns and local file paths. Image-regeneration admission/replay responses expose only their opaque operation ID for correlation, not internal operation metadata. Plaintext keys live in the private config file/bucket; task snapshots and data bucket objects contain model settings and credential references only. Request/response documentation contains schemas only, never live values.

## Persistence and verification

Configuration lives outside the runtime data directory. With private config/data buckets enabled, every database write is followed by a SQLite backup and content-addressed media uploads; `latest.json` is published and read back last. Failed persistence returns 503, makes health checks fail and stops task admission until restart. Startup restores only a complete, hash-verified snapshot and marks in-flight work interrupted. It never automatically retries a paid provider request. This is a single-writer design: do not run multiple instances against the same buckets.

`session.ephemeral` is false when data snapshots are configured. Without a data bucket or an independent durable backup directory, an HF container rebuild can still lose tasks and quota counters. Before migrating a legacy ephemeral Space, obtain the complete database and media; exporting configuration or downloading videos alone does not preserve task state.

`tests/test_api_pipeline.py` performs the full task, media approval, FFmpeg composition, download and administrator-config sequence through real HTTP only, without requesting a page. Model/image/audio are offline fixtures in this regression, not evidence that a real provider accepts the selected key/model.
