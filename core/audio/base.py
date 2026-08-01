from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class ProviderUnavailable(RuntimeError):
    pass


@dataclass
class ProviderStatus:
    available: bool
    source: str
    details: dict[str, Any]
