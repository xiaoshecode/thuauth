"""Small immutable types shared by configuration and protocol code."""

from dataclasses import dataclass, field
from enum import StrEnum
from time import monotonic
from typing import Literal

type TargetFamily = Literal["ipv4", "ipv6", "both"]
type IPFamily = Literal["ipv4", "ipv6"]


@dataclass(frozen=True, slots=True)
class Credentials:
    username: str = field(default="", repr=False)
    password: str = field(default="", repr=False)


@dataclass(frozen=True, slots=True)
class Target:
    url: str = field(repr=False)
    family: TargetFamily = "ipv4"


@dataclass(frozen=True, slots=True)
class Challenge:
    token: str = field(repr=False)
    reported_ip: str | None = field(default=None, repr=False)


class AuthReplyKind(StrEnum):
    ACCEPTED = "accepted"
    BLOCKED = "blocked"
    THROTTLED = "throttled"
    ALREADY_ONLINE = "already_online"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class AuthReply:
    """Only locally defined categories, codes and messages; never a raw server reply."""

    kind: AuthReplyKind
    code: str | None
    message: str


class OnlineState(StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class AccessContext:
    family: IPFamily
    ac_id: str
    ip: str = field(default="", repr=False)


@dataclass(frozen=True, slots=True)
class OnlineObservation:
    family: IPFamily
    state: OnlineState
    username: str | None = field(default=None, repr=False)
    ip: str | None = field(default=None, repr=False)
    reason: str = ""
    observed_at: float = field(default_factory=monotonic)
