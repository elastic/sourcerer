"""SWE-Explore-Bench benchmark handler.

Exposes the three hooks the generic benchmark command dispatches to:
`get` / `is_ready` (dataset download + build) and `run` (the eval).
"""
from .dataset import get, is_ready
from .eval import run

__all__ = ["get", "is_ready", "run"]
