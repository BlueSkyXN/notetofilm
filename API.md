# NoteToFilm API v1

All business functions use `/api/v1`. HTML/CSS/JavaScript are static clients: no server-injected task IDs, configuration or credentials. FastAPI can serve the bundled frontend, or a reverse proxy can serve these files separately while routing `/api/v1/*` to the backend under the same origin. There is no requirement to open a page to use the API.

- Read-only API explorer: `/api-docs` (no external CDN, no automatic inference).
- Machine-readable request/response schemas: `/api/v1/openapi.json`.
- Health: `GET /api/v1/health`; `/healthz` remains the container health-check alias.
- Pre-v1 `/api/*` and `/files/*` routes are removed. Frontend and backend must be upgraded together.
- This remains one process and one data volume, not a distributed or multi-worker service.

## Authentication and isolation

Anonymous users do not log in. A client first calls `GET /api/v1/session`, then preserves `ntf_visitor` in its cookie jar. The signed HttpOnly cookie identifies the visitor and expires after 30 days. Clearing it or switching devices loses access to the visitor's existing tasks. All task metadata, writes and media downloads enforce ownership; another visitor receives 404. An HTML shell being public does not grant access to its task data.

Administrators call `POST /api/v1/auth/login` with `{"token":"<administrator token>"}` and retain `ntf_admin`, or send `X-Access-Token`. Obtain this token from your secret manager; do not put it in a URL, checked-in client, example file or command history. `POST /api/v1/auth/logout` clears the administrator browser cookie. It does not revoke previously copied cookies or the header token; rotating the deployment token invalidates both admin and visitor signatures.

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
| PUT | `/tasks/{id}/plan` | Edit plan with expected revision |
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
| GET | `/admin/config/export` | Download the saved, active TOML; never includes key values |

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

`POST /tasks/{id}/retry` with `{"confirmed":false}` may return 200 with `accepted:false` and `needs_confirm:true`; ask the user before repeating with `confirmed:true`. A timeout can mean an upstream call was billed. There are no automatic retries, and clients must not automatically retry mutation calls. Server-side ownership, revisions, active-operation checks and duplicate-script protection remain in force; this is not a universal idempotency-key implementation.

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

`X-Request-ID` matches the body. Input validation reports field locations, not the original input values. Unexpected errors expose neither stack traces nor upstream responses. Codes include `admin_required`, `forbidden`, `not_found`, `conflict`, `validation_error`, `quota_exceeded`, `unavailable`, `insufficient_storage`.

Response schemas whitelist public fields. Task responses omit owner identifiers, raw configuration snapshots, internal operation IDs, database columns and local file paths. Model keys remain in environment/Secrets, configuration files store only approved key references. Request/response documentation contains schemas only, never live values.

## Persistence and verification

Configuration and task state live on the data volume. HF's current ephemeral disk can reset tasks, configuration and quota counters. Static frontend separation does not solve this; use durable storage with verified SQLite semantics or export configuration/download videos before restart.

`tests/test_api_pipeline.py` performs the full task, media approval, FFmpeg composition, download and administrator-config sequence through real HTTP only, without requesting a page. Model/image/audio are offline fixtures in this regression, not evidence that a real provider accepts the selected key/model.
