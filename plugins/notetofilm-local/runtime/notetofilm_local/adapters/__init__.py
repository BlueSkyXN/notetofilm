"""Explicit protocol registry. Model names never choose an adapter."""
from ..errors import NTFError


def get_adapter(route: dict):
    from .openai_images import OpenAIImagesAdapter
    from .ark_video import ArkVideoAdapter
    from .gemini_veo import GeminiVeoAdapter
    adapters = {"openai-images": OpenAIImagesAdapter, "ark-video": ArkVideoAdapter, "gemini-veo": GeminiVeoAdapter}
    name = route.get("adapter")
    if name not in adapters:
        raise NTFError("adapter_unconfigured", "Select an implemented protocol that matches the company's actual API; no protocol is inferred from the model name.", {"adapter": name, "implemented": sorted(adapters)})
    cls = adapters[name]
    if route.get("modality") != cls.modality:
        raise NTFError("route_modality", "The selected protocol does not implement this modality.")
    return cls(route)


def list_adapters() -> list:
    return [
        {"id": "openai-images", "modality": "image", "version": 1, "company_connectivity": "unconfirmed"},
        {"id": "ark-video", "modality": "video", "version": 1, "company_connectivity": "unconfirmed"},
        {"id": "gemini-veo", "modality": "video", "version": 1, "company_connectivity": "unconfirmed"},
    ]
