"""A single named rule result. Shared by the engine and the message checks."""

from dataclasses import dataclass


@dataclass
class Check:
    rule: str
    passed: bool
    reason: str
