"""Provider drivers. Add one by implementing BurstDriver and registering it here."""
from .base import BurstDriver, BurstNode, Offer, Phase
from .runpod import RunPodDriver

DRIVERS = {"runpod": RunPodDriver}


def get_driver(name: str, **kw) -> BurstDriver:
    if name not in DRIVERS:
        raise ValueError(f"unknown provider {name!r} (have {sorted(DRIVERS)})")
    return DRIVERS[name](**kw)


__all__ = ["BurstDriver", "BurstNode", "Offer", "Phase", "DRIVERS", "get_driver"]
