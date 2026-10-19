"""Explicit HTTP transport for configured company protocols; never retries POST."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid

from ..capabilities import check_request, request_mode, request_parameters
from ..errors import NTFError


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def origin(url: str) -> tuple:
    p = urlsplit(url)
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
        raise NTFError("url_invalid", "Media and API addresses must be HTTP(S) URLs without embedded credentials.")
    try:
        return (p.scheme, p.hostname.lower(), p.port or (443 if p.scheme == "https" else 80))
    except ValueError:
        raise NTFError("url_invalid", "Invalid port in an API or media address.") from None


def image_mime(raw: bytes) -> str:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        return "image/webp"
    raise NTFError("image_format", "Reference must be a PNG, JPEG or WebP file.")


def local_image(ref: dict, project_root: Path, max_bytes: int = 50 * 1024 * 1024) -> tuple[bytes, str]:
    if not isinstance(ref, dict) or not isinstance(ref.get("path"), str):
        raise NTFError("reference_missing", "Resolve the reference to a local project path before generation.")
    root = Path(project_root).resolve()
    source = Path(ref["path"])
    source = (root / source).resolve() if not source.is_absolute() else source.resolve()
    if not source.is_relative_to(root):
        raise NTFError("path_outside_project", "Import the reference into this project before generation.")
    try:
        if not source.is_file() or source.stat().st_size > max_bytes:
            raise NTFError("reference_size", "Reference is missing or exceeds the configured local input size limit.")
        raw = source.read_bytes()
    except OSError:
        raise NTFError("reference_missing", "Cannot read the referenced project file.") from None
    return raw, image_mime(raw)


def reference_prompt(request: dict) -> str:
    prompt = next(request[k] for k in ("prompt", "visual_prompt", "video_prompt") if k in request)
    notes = []
    for i, ref in enumerate(request.get("references", []), 1):
        role, instruction = ref.get("role"), ref.get("instruction")
        if role or instruction:
            if any(v is not None and not isinstance(v, str) for v in (role, instruction)):
                raise NTFError("reference_invalid", "Reference role and instruction must be strings.")
            notes.append(f"Reference image {i}" + (f" ({role})" if role else "") + (f": {instruction}" if instruction else ""))
    for key, label in (("start_frame", "Starting frame"), ("end_frame", "Ending frame")):
        ref = request.get(key)
        if isinstance(ref, dict) and ref.get("instruction"):
            if not isinstance(ref["instruction"], str):
                raise NTFError("reference_invalid", "Frame instructions must be strings.")
            notes.append(label + ": " + ref["instruction"])
    return prompt + ("\n\nReference-image instructions (in submitted order):\n" + "\n".join(notes) if notes else "")


def multipart(fields: dict, images: list[tuple[bytes, str]]) -> tuple[bytes, str]:
    boundary = "ntf-" + uuid.uuid4().hex
    body = bytearray()
    for key, value in fields.items():
        body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
        body.extend((str(value).lower() if isinstance(value, bool) else str(value)).encode("utf-8"))
        body.extend(b"\r\n")
    for i, (raw, mime) in enumerate(images, 1):
        suffix = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}[mime]
        body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="image[]"; filename="reference-{i}.{suffix}"\r\nContent-Type: {mime}\r\n\r\n'.encode())
        body.extend(raw)
        body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode())
    return bytes(body), f"multipart/form-data; boundary={boundary}"


class BaseAdapter:
    protocol = "base"
    version = 1
    modality = ""

    def __init__(self, route: dict):
        self.route = route
        self.connection = route.get("connection", {})
        if not isinstance(self.connection, dict):
            raise NTFError("connection_missing", "Resolve the route connection before creating the adapter.")
        self.base_url = self.connection.get("base_url") or os.environ.get(self.connection.get("base_url_env", ""))
        if not self.base_url:
            raise NTFError("connection_missing", "Set the configured company base URL; no original-provider URL is substituted.")
        self.base_url = self.base_url.rstrip("/")
        origin(self.base_url)
        if urlsplit(self.base_url).query or urlsplit(self.base_url).fragment:
            raise NTFError("connection_invalid", "The company base URL cannot contain query credentials or a fragment.")
        self.timeout = float(self.connection.get("timeout_seconds", 120))
        self.max_json_bytes = int(self.connection.get("max_json_bytes", 128 * 1024 * 1024))
        self.max_download_bytes = int(self.connection.get("max_download_bytes", 256 * 1024 * 1024))
        if self.timeout <= 0 or self.max_json_bytes <= 0 or self.max_download_bytes <= 0:
            raise NTFError("connection_invalid", "Timeout and response size limits must be positive.")
        self.opener = build_opener(NoRedirect())

    def describe(self) -> dict:
        return {"adapter": self.protocol, "version": self.version, "modality": self.modality,
                "capabilities": self.route.get("capabilities", {}), "connectivity": "not_tested"}

    def endpoint(self, name: str, default: str, **values) -> str:
        paths = {**self.connection.get("paths", {}), **self.route.get("paths", {})}
        template = paths.get(name, default)
        if not isinstance(template, str):
            raise NTFError("path_invalid", "Protocol endpoint path must be a string.")
        encoded = {k: quote(str(v), safe="/" if k == "operation" else "") for k, v in values.items()}
        try:
            path = template.format(**encoded)
        except (KeyError, ValueError):
            raise NTFError("path_invalid", "Unsupported placeholder in the configured endpoint path.") from None
        if any(part == ".." for part in urlsplit(path).path.split("/")):
            raise NTFError("path_invalid", "Endpoint paths cannot traverse to parent paths.")
        url = path if urlsplit(path).scheme else self.base_url + "/" + path.lstrip("/")
        if origin(url) != origin(self.base_url):
            raise NTFError("endpoint_origin", "Submit and poll endpoints must belong to the explicitly configured company origin.")
        return url

    def _auth_headers(self) -> dict:
        auth = self.connection.get("auth", {})
        kind = auth.get("type", "unconfirmed")
        if kind not in ("none", "bearer", "header"):
            raise NTFError("auth_unconfirmed", "Configure the company's actual authentication type: bearer, header, or none.")
        headers = {}
        if kind != "none":
            env = auth.get("env")
            secret = os.environ.get(env or "")
            if not secret:
                raise NTFError("credential_missing", "The configured credential environment variable is not set.", {"environment_variable": env})
            header = auth.get("header", "Authorization" if kind == "bearer" else None)
            if not header:
                raise NTFError("auth_invalid", "Header authentication requires an explicit header name.")
            headers[header] = (auth.get("prefix", "Bearer ") if kind == "bearer" else auth.get("prefix", "")) + secret
        for name, env_name in self.connection.get("header_envs", {}).items():
            value = os.environ.get(env_name, "")
            if not value:
                raise NTFError("credential_missing", "A configured header environment variable is not set.", {"environment_variable": env_name})
            headers[name] = value
        if any("\r" in str(v) or "\n" in str(v) for pair in headers.items() for v in pair):
            raise NTFError("auth_invalid", "Header names and values cannot contain line breaks.")
        return headers

    def _read(self, method: str, url: str, body: bytes | None = None, content_type: str | None = None, *, download=False) -> bytes:
        limit = self.max_download_bytes if download else self.max_json_bytes
        current = url
        for hop in range(6):
            same_origin = origin(current) == origin(self.base_url)
            if not download and not same_origin:
                raise NTFError("endpoint_origin", "API operations cannot change company origin.")
            headers = {"User-Agent": "NoteToFilm-local/0.1"}
            # Never forward company credentials to object-storage or other redirect origins.
            if same_origin:
                headers.update(self._auth_headers())
            if content_type:
                headers["Content-Type"] = content_type
            req = Request(current, data=body, headers=headers, method=method)
            try:
                with self.opener.open(req, timeout=self.timeout) as response:
                    raw = response.read(limit + 1)
                if len(raw) > limit:
                    code = "submission_unknown" if method == "POST" else "response_too_large"
                    raise NTFError(code, "Upstream response exceeds the configured local size limit; no request was retried.")
                return raw
            except HTTPError as exc:
                status = exc.code
                location = exc.headers.get("Location")
                exc.close()
                if download and method == "GET" and status in (301, 302, 303, 307, 308) and location:
                    current = urljoin(current, location)
                    continue
                code = "submission_unknown" if method == "POST" and (status >= 500 or status in (408, 301, 302, 303, 307, 308)) else "upstream_http"
                raise NTFError(code, "Company request did not return a usable result; it was not automatically retried.", {"status": status, "method": method}) from None
            except (URLError, TimeoutError, socket.timeout, OSError):
                code = "submission_unknown" if method == "POST" else "network_error"
                raise NTFError(code, "The request outcome could not be obtained; no automatic retry was sent.", {"method": method}) from None
        raise NTFError("download_redirects", "Too many media download redirects.")

    def _json(self, method: str, url: str, payload: dict | None = None, *, body=None, content_type=None) -> dict:
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
            content_type = "application/json"
        raw = self._read(method, url, body, content_type)
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError()
        except (ValueError, UnicodeError):
            code = "submission_unknown" if method == "POST" else "upstream_format"
            raise NTFError(code, "The endpoint did not return a JSON object; it was not retried.") from None
        return data

    def _prepare(self, request: dict, allowed: set[str]) -> tuple[str, str, dict]:
        check_request(self.route, request)
        params = request_parameters(request, self.route.get("defaults", {}))
        unknown = sorted(set(params) - allowed)
        if unknown:
            raise NTFError("unsupported_parameter", "This protocol adapter cannot send these parameters; none were silently discarded.", {"parameters": unknown, "adapter": self.protocol})
        return request_mode(self.route, request), reference_prompt(request), params

    def _job(self, remote_id: str, poll_url: str) -> dict:
        return {"remote_id": remote_id, "poll_url": poll_url, "route_id": self.route.get("id"),
                "model": self.route.get("model"), "adapter": self.protocol, "adapter_version": self.version,
                "base_url": self.base_url}

    def _poll_url(self, job: dict) -> str:
        if job.get("adapter") != self.protocol or job.get("route_id") != self.route.get("id") or job.get("model") != self.route.get("model"):
            raise NTFError("job_route_mismatch", "Resume this task using its saved original route, model and adapter.")
        if job.get("adapter_version") != self.version or job.get("base_url") != self.base_url:
            raise NTFError("job_route_mismatch", "The original task connection/protocol changed; restore its saved route before polling.")
        url = job.get("poll_url")
        if not isinstance(url, str) or origin(url) != origin(self.base_url):
            raise NTFError("job_invalid", "Task polling URL does not belong to its original company connection.")
        return url

    def poll(self, job: dict) -> dict:
        raise NTFError("poll_unsupported", "This protocol returns synchronous results and has no task polling operation.")

    def fetch(self, result: dict, output_dir: Path) -> list:
        if result.get("status") != "succeeded" or not result.get("outputs"):
            raise NTFError("result_not_ready", "No completed media outputs are available to download.")
        target = Path(output_dir).resolve()
        target.mkdir(parents=True, exist_ok=True)
        artifacts = []
        for i, output in enumerate(result["outputs"], 1):
            kind = output.get("kind", self.modality)
            if output.get("base64"):
                encoded = output["base64"]
                if not isinstance(encoded, str) or len(encoded) > (self.max_download_bytes * 4 // 3 + 8):
                    raise NTFError("media_size", "Encoded media exceeds the configured local limit.")
                try:
                    raw = base64.b64decode(encoded, validate=True)
                except (ValueError, TypeError):
                    raise NTFError("media_format", "The completed result has invalid base64 media.") from None
            elif isinstance(output.get("url"), str):
                raw = self._read("GET", output["url"], download=True)
            else:
                raise NTFError("media_missing", "A completed output has no downloadable URL or media bytes.")
            if not raw or len(raw) > self.max_download_bytes:
                raise NTFError("media_size", "The completed media is empty or exceeds the configured local limit.")
            if kind == "image":
                mime = image_mime(raw)
                suffix = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}[mime]
            elif kind == "video" and len(raw) >= 12 and raw[4:8] == b"ftyp":
                suffix = ".mp4"
            elif kind == "video" and raw.startswith(b"\x1a\x45\xdf\xa3"):
                suffix = ".webm"
            else:
                raise NTFError("media_format", "Downloaded content is not a supported image or MP4/WebM container.")
            digest = hashlib.sha256(raw).hexdigest()
            path = target / f"{kind}-{i:02d}-{digest[:16]}{suffix}"
            if not path.exists():
                fd, temporary = tempfile.mkstemp(prefix=".download-", dir=target)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(raw)
                    os.replace(temporary, path)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
            artifact = {"path": str(path), "kind": kind, "sha256": digest}
            if output.get("role"):
                artifact["role"] = output["role"]
            artifacts.append(artifact)
        return artifacts
