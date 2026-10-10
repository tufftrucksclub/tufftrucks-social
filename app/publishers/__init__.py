from .base import Metrics, PublishError, Publisher, Result
from .meta import Facebook, Instagram
from .pinterest import Pinterest
from .tiktok import TikTok
from .x import X

REGISTRY: dict[str, Publisher] = {
    "instagram": Instagram(),
    "facebook": Facebook(),
    "tiktok": TikTok(),
    "pinterest": Pinterest(),
    "x": X(),
}


def get(platform: str) -> Publisher:
    return REGISTRY[platform]
