"""Ark content-generation tasks protocol, only when selected for a company route."""
import base64
from pathlib import Path

from ..errors import NTFError
from .base import BaseAdapter, local_image


class ArkVideoAdapter(BaseAdapter):
    protocol = "ark-video"
    modality = "video"
    PARAMS = {"duration_seconds", "aspect_ratio", "resolution", "seed", "camera_fixed", "watermark", "generate_audio", "return_last_frame", "service_tier", "execution_expires_after", "count"}

    def submit(self, request: dict, project_root: Path) -> dict:
        mode, prompt, params = self._prepare(request, self.PARAMS)
        if params.get("count", 1) != 1:
            raise NTFError("unsupported_parameter", "Ark tasks produce one video per submission; schedule separate actions for multiple candidates.")
        content = [{"type": "text", "text": prompt}]
        inputs = []
        if mode == "reference_to_video":
            inputs = [(ref, "reference_image") for ref in request["references"]]
        elif mode == "image_to_video":
            inputs = [(request["start_frame"], "first_frame")]
        elif mode == "first_last_to_video":
            inputs = [(request["start_frame"], "first_frame"), (request["end_frame"], "last_frame")]
        for ref, role in inputs:
            raw, mime = local_image(ref, project_root, int(self.connection.get("max_input_image_bytes", 50 * 1024 * 1024)))
            content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")}, "role": role})
        payload = {"model": self.route["model"], "content": content}
        mapping = {"duration_seconds": "duration", "aspect_ratio": "ratio"}
        payload.update({mapping.get(k, k): v for k, v in params.items() if k != "count"})
        response = self._json("POST", self.endpoint("submit", "contents/generations/tasks"), payload)
        if response.get("error") and not response.get("id"):
            return {"status": "failed", "outputs": [], "error": {"code": "generation_failed", "message": "The Ark-compatible endpoint rejected video generation."}}
        remote_id = response.get("id")
        if not isinstance(remote_id, str) or not remote_id:
            raise NTFError("submission_unknown", "Video submission returned no Ark task ID; do not repeat the paid submission automatically.")
        job = self._job(remote_id, self.endpoint("poll", "contents/generations/tasks/{task_id}", task_id=remote_id))
        try:
            return self._normalize(response, job)
        except NTFError as exc:
            # A valid company handle must reach the caller even when optional
            # immediate status/content has an unexpected shape.
            return {"status": "submitted", "job": job, "outputs": [], "observation": exc.as_dict()}

    def poll(self, job: dict) -> dict:
        response = self._json("GET", self._poll_url(job))
        if response.get("id") not in (None, job["remote_id"]):
            raise NTFError("job_mismatch", "The query returned a different task ID.")
        return self._normalize(response, job)

    def _normalize(self, response: dict, job: dict) -> dict:
        status = response.get("status", "queued")
        result = {"job": job, "outputs": [], "remote_status": status}
        if not isinstance(status, str):
            raise NTFError("upstream_status", "Ark task status must be a string.")
        if status in ("queued", "running"):
            return {**result, "status": "submitted"}
        if status in ("failed", "cancelled", "canceled", "expired"):
            return {**result, "status": "failed", "error": {"code": "generation_failed", "message": f"Company task ended with status {status}."}}
        if status != "succeeded":
            raise NTFError("upstream_status", "The configured Ark-compatible route returned an unknown task status.")
        content = response.get("content", {})
        if not isinstance(content, dict) or not isinstance(content.get("video_url"), str):
            raise NTFError("upstream_format", "The completed task has no content.video_url; retain its ID and query again before considering another submission.")
        result.update({"status": "succeeded", "outputs": [{"kind": "video", "url": content["video_url"]}], "model": response.get("model")})
        if isinstance(content.get("last_frame_url"), str):
            result["outputs"].append({"kind": "image", "url": content["last_frame_url"], "role": "last_frame"})
        return result
