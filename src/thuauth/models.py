"""Small immutable types shared by configuration and future protocol code."""

from dataclasses import dataclass, field
from typing import Literal

type TargetFamily = Literal["ipv4", "ipv6", "both"]


@dataclass(frozen=True, slots=True)
class Credentials:
    username: str = field(default="", repr=False)
    password: str = field(default="", repr=False)


@dataclass(frozen=True, slots=True)
class Target:
    url: str = field(repr=False)
    family: TargetFamily = "ipv4"
