"""Gemini predictLongRunning Veo REST protocol, on a configured company origin."""
import base64
from pathlib import Path

from ..errors import NTFError
from .base import BaseAdapter, local_image


class GeminiVeoAdapter(BaseAdapter):
    protocol = "gemini-veo"
    modality = "video"
    PARAMS = {"duration_seconds", "aspect_ratio", "resolution", "seed", "count", "negative_prompt", "person_generation"}

    def _image(self, ref: dict, project_root: Path) -> dict:
        raw, mime = local_image(ref, project_root, int(self.connection.get("max_input_image_bytes", 50 * 1024 * 1024)))
        return {"inlineData": {"mimeType": mime, "data": base64.b64encode(raw).decode("ascii")}}

    def submit(self, request: dict, project_root: Path) -> dict:
        mode, prompt, params = self._prepare(request, self.PARAMS)
        instance = {"prompt": prompt}
        if mode == "reference_to_video":
            # Local subject/style/scene roles guide the prompt; REST referenceType
            # is 'asset'. It does not turn local role labels into provider controls.
            instance["referenceImages"] = [{"image": self._image(ref, project_root), "referenceType": "asset"} for ref in request["references"]]
        elif mode in ("image_to_video", "first_last_to_video"):
            instance["image"] = self._image(request["start_frame"], project_root)
            if mode == "first_last_to_video":
                instance["lastFrame"] = self._image(request["end_frame"], project_root)
        payload = {"instances": [instance]}
        mapping = {"duration_seconds": "durationSeconds", "aspect_ratio": "aspectRatio", "count": "sampleCount", "negative_prompt": "negativePrompt", "person_generation": "personGeneration"}
        if params:
            payload["parameters"] = {mapping.get(k, k): v for k, v in params.items()}
        response = self._json("POST", self.endpoint("submit", "models/{model}:predictLongRunning", model=self.route["model"]), payload)
        if response.get("error") and not response.get("name"):
            return {"status": "failed", "outputs": [], "error": {"code": "generation_failed", "message": "The Gemini-compatible endpoint rejected generation."}}
        name = response.get("name")
        if not isinstance(name, str) or not name:
            raise NTFError("submission_unknown", "Video submission returned no Gemini operation name; do not repeat the paid submission automatically.")
        job = self._job(name, self.endpoint("poll", "{operation}", operation=name))
        job["operation_name"] = name
        try:
            return self._normalize(response, job)
        except NTFError as exc:
            return {"status": "submitted", "job": job, "outputs": [], "observation": exc.as_dict()}

    def poll(self, job: dict) -> dict:
        response = self._json("GET", self._poll_url(job))
        if response.get("name") not in (None, job["remote_id"]):
            raise NTFError("job_mismatch", "The query returned a different operation name.")
        return self._normalize(response, job)

    def _normalize(self, response: dict, job: dict) -> dict:
        if response.get("error"):
            return {"status": "failed", "job": job, "outputs": [], "error": {"code": "generation_failed", "message": "The configured Gemini operation returned an error."}}
        if response.get("done", False) is False:
            return {"status": "submitted", "job": job, "outputs": []}
        if response.get("done") is not True:
            raise NTFError("upstream_format", "Gemini operation.done must be a boolean.")
        content = response.get("response", {})
        if not isinstance(content, dict) or not isinstance(content.get("generateVideoResponse", {}), dict):
            raise NTFError("upstream_format", "The completed operation has an invalid generateVideoResponse object.")
        generated = content.get("generateVideoResponse", {})
        samples = generated.get("generatedSamples", [])
        if not isinstance(samples, list) or not samples:
            return {"status": "failed", "job": job, "outputs": [], "error": {"code": "generation_empty", "message": "The completed Gemini operation returned no generated video samples."}}
        outputs = []
        for sample in samples:
            video = sample.get("video", {}) if isinstance(sample, dict) else {}
            if not isinstance(video, dict) or not isinstance(video.get("uri"), str):
                raise NTFError("upstream_format", "The completed Gemini response has no video.uri; retain the operation for recovery.")
            outputs.append({"kind": "video", "url": video["uri"]})
        return {"status": "succeeded", "job": job, "outputs": outputs}
