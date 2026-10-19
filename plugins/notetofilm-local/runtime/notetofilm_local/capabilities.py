"""Request semantics plus company-declared capability checks, with no model inference."""
from __future__ import annotations

import math
from .errors import NTFError

IMAGE_MODES = {"text_to_image", "reference_to_image"}
VIDEO_MODES = {"text_to_video", "image_to_video", "reference_to_video", "first_last_to_video"}
ALIASES = {"n": "count", "duration": "duration_seconds", "ratio": "aspect_ratio", "format": "output_format"}
META = {"action_id", "scene_id", "route", "kind", "mode", "prompt", "visual_prompt", "video_prompt", "references", "start_frame", "end_frame", "parameters", "output", "duration_seconds", "model", "selection_slot"}


def request_mode(route: dict, request: dict) -> str:
    if request.get("mode"):
        return request["mode"]
    if route.get("modality") == "image":
        return "reference_to_image" if request.get("references") else "text_to_image"
    if request.get("end_frame"):
        return "first_last_to_video"
    if request.get("start_frame"):
        return "image_to_video"
    return "reference_to_video" if request.get("references") else "text_to_video"


def request_parameters(request: dict, defaults: dict | None = None) -> dict:
    params = {}
    def merge(items, replacing=False):
        if not isinstance(items, dict):
            raise NTFError("request_invalid", "parameters and output must be objects.")
        for key, value in items.items():
            canonical = ALIASES.get(key, key)
            if canonical in params and params[canonical] != value and not replacing:
                raise NTFError("parameter_conflict", "The same generation parameter has conflicting values.", {"parameter": canonical})
            params[canonical] = value
    merge(defaults or {})
    merge(request.get("parameters", {}), replacing=True)
    output = dict(request.get("output", {}))
    if "width" in output or "height" in output:
        if any(type(output.get(k)) is not int or output[k] <= 0 for k in ("width", "height")):
            raise NTFError("request_invalid", "Output width and height must both be positive integers.")
        output["size"] = f'{output.pop("width")}x{output.pop("height")}'
    merge(output)
    if "duration_seconds" in request:
        merge({"duration_seconds": request["duration_seconds"]})
    return params


def _value_matches(value, rule: dict) -> bool:
    if not isinstance(rule, dict):
        return False
    expected = rule.get("type")
    types = {"string": str, "integer": int, "number": (int, float), "boolean": bool}
    if expected in types and (not isinstance(value, types[expected]) or (expected in ("integer", "number") and isinstance(value, bool))):
        return False
    if isinstance(value, (int, float)) and (isinstance(value, float) and not math.isfinite(value)):
        return False
    if "enum" in rule and value not in rule["enum"]:
        return False
    if "minimum" in rule and (not isinstance(value, (int, float)) or value < rule["minimum"]):
        return False
    if "maximum" in rule and (not isinstance(value, (int, float)) or value > rule["maximum"]):
        return False
    return True


def check_request(route: dict, request: dict) -> None:
    if not isinstance(request, dict):
        raise NTFError("request_invalid", "Generation request must be an object.")
    unknown = sorted(set(request) - META)
    if unknown:
        raise NTFError("unsupported_parameter", "Unknown request fields were not sent to the company API.", {"fields": unknown})
    if request.get("model") not in (None, route.get("model")):
        raise NTFError("model_conflict", "Select another route to change models; request.model cannot override the route.")
    if request.get("kind") not in (None, route.get("modality")):
        raise NTFError("route_modality", "Request kind differs from the route modality.")
    mode = request_mode(route, request)
    modes = IMAGE_MODES if route.get("modality") == "image" else VIDEO_MODES
    if mode not in modes:
        raise NTFError("unsupported_mode", "This mode belongs to a different or unimplemented modality.", {"mode": mode})
    prompts = [request[k] for k in ("prompt", "visual_prompt", "video_prompt") if k in request]
    if not prompts or any(not isinstance(p, str) or not p.strip() for p in prompts) or len(set(prompts)) != 1:
        raise NTFError("request_invalid", "Provide one non-empty, unambiguous prompt.")
    refs = request.get("references", [])
    if not isinstance(refs, list):
        raise NTFError("request_invalid", "references must be an ordered list.")
    for i, ref in enumerate(refs, 1):
        if not isinstance(ref, dict) or ref.get("position", i) != i:
            raise NTFError("reference_order", "Reference positions must match their list order; no automatic reordering is performed.")
        if ref.get("role") in {"first_frame", "last_frame", "start_frame", "end_frame"}:
            raise NTFError("reference_role", "Use start_frame/end_frame fields for endpoint frames, not ordinary references.")
    start, end = request.get("start_frame"), request.get("end_frame")
    expected = {
        "text_to_image": (False, False, False), "reference_to_image": (True, False, False),
        "text_to_video": (False, False, False), "image_to_video": (False, True, False),
        "reference_to_video": (True, False, False), "first_last_to_video": (False, True, True),
    }[mode]
    if (bool(refs), bool(start), bool(end)) != expected:
        raise NTFError("mode_inputs", "References and endpoint frames do not match the selected mode; they are not interchangeable.", {"mode": mode})
    cap = route.get("capabilities", {})
    if not isinstance(cap, dict) or cap.get("status") != "confirmed":
        raise NTFError("capability_unknown", "Company route capabilities are not confirmed; configure them from the company's contract before generation.", {"route": route.get("id")})
    spec = cap.get("modes", {}).get(mode)
    if not isinstance(spec, dict) or spec.get("supported") is not True:
        code = "unsupported_mode" if isinstance(spec, dict) and spec.get("supported") is False else "capability_unknown"
        raise NTFError(code, "The selected company route does not declare this mode available.", {"route": route.get("id"), "mode": mode})
    if refs:
        limit = spec.get("max_reference_images")
        if type(limit) is not int:
            raise NTFError("capability_unknown", "Confirm max_reference_images for this company route.")
        if len(refs) > limit:
            raise NTFError("reference_limit", "The request exceeds the configured company reference limit.", {"requested": len(refs), "maximum": limit})
    params = request_parameters(request, route.get("defaults", {}))
    rules = spec.get("parameters", {})
    for name, value in params.items():
        if name not in rules:
            raise NTFError("capability_unknown", "A requested parameter is not declared for this company mode.", {"parameter": name, "mode": mode})
        if not _value_matches(value, rules[name]):
            raise NTFError("unsupported_parameter", "A parameter value is outside the configured company capability.", {"parameter": name, "mode": mode})
    missing = [key for key in spec.get("required_parameters", []) if key not in params]
    if missing:
        raise NTFError("parameter_missing", "The company mode requires explicit parameters.", {"parameters": missing})
    for combination in spec.get("constraints", []):
        if all(params.get(k) == v for k, v in combination.get("when", {}).items()):
            for key, allowed in combination.get("require", {}).items():
                if params.get(key) not in allowed:
                    raise NTFError("parameter_combination", "This parameter combination is not supported by the configured company mode.", {"parameter": key, "when": combination.get("when", {})})
