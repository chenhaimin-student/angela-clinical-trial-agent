"""Core models and pure helpers for Angela's idle session compactor."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


CHANNEL_SOURCES = ("dingtalk", "wecom", "wecom_callback")


@dataclass(frozen=True)
class TranscriptFingerprint:
    count: int
    max_id: int
    last_timestamp: float


@dataclass(frozen=True)
class Candidate:
    session_id: str
    session_key: str
    source: str
    fingerprint: TranscriptFingerprint
    approx_tokens: int
    user_turns: int


def estimate_message_tokens(messages: list[dict[str, Any]]) -> int:
    """Conservative content-only estimate used for scheduling and audit."""
    total = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            chars = len(content)
        else:
            chars = len(json.dumps(content, ensure_ascii=False, default=str))
        total += max(1, (chars + 3) // 4)
    return total


def keep_schedule(user_turns: int) -> tuple[int, ...]:
    """Choose increasingly aggressive, still-bounded recent-turn policies."""
    if user_turns > 10:
        return (10, 5, 3)
    if user_turns > 5:
        return (5, 3)
    if user_turns > 3:
        return (3,)
    return ()


def process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        import os

        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True
