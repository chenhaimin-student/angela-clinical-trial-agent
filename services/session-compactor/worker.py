#!/usr/bin/env python3
"""Compress exactly one session; stdout contains one JSON metadata result."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
import uuid
from pathlib import Path

from gateway.run import GatewayRunner, _load_gateway_config
from hermes_cli.partial_compress import (
    rejoin_compressed_head_and_tail,
    split_history_for_partial_compress,
)
from hermes_state import SessionDB
from run_agent import AIAgent

from session_compactor_models import estimate_message_tokens
from session_compactor_store import guarded_archive, transcript_fingerprint


FOCUS = (
    "Preserve patient facts, CTR identifiers, diseases, age, sex, treatment history, "
    "dates, city, questionnaire answers, unknown fields, decisions, user preferences, "
    "tool findings, and unresolved tasks. Do not invent missing facts."
)


def _terminate_cleanly(_signum: int, _frame: object) -> None:
    # Raising SystemExit unwinds the try/finally below, releasing the durable
    # Hermes compression lock before the scheduler starts a retry.
    raise SystemExit(143)


def emit(**data: object) -> None:
    print(json.dumps(data, ensure_ascii=False, separators=(",", ":")), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("session_id")
    parser.add_argument("--db", required=True)
    parser.add_argument("--keep", type=int, required=True)
    args = parser.parse_args()

    signal.signal(signal.SIGTERM, _terminate_cleanly)
    signal.signal(signal.SIGINT, _terminate_cleanly)

    started = time.monotonic()
    db = SessionDB(Path(args.db))
    holder = f"session-compactor:{os.getpid()}:{uuid.uuid4().hex}"
    if not db.try_acquire_compression_lock(args.session_id, holder, ttl_seconds=900):
        emit(status="locked", session_id=args.session_id)
        return 3
    try:
        before = transcript_fingerprint(db._conn, args.session_id)
        history = db.get_messages_as_conversation(args.session_id, repair_alternation=True)
        before_tokens = estimate_message_tokens(history)
        head, tail = split_history_for_partial_compress(history, args.keep)
        if not tail or len(head) < 4:
            emit(status="no_boundary", session_id=args.session_id, keep=args.keep)
            return 4

        runner = GatewayRunner()
        model, runtime = runner._resolve_session_agent_runtime(
            source=None, session_key=None, user_config=_load_gateway_config()
        )
        agent = AIAgent(
            **runtime,
            model=model,
            max_iterations=4,
            quiet_mode=True,
            skip_memory=True,
            enabled_toolsets=["memory"],
            session_id=args.session_id,
            session_db=None,
        )
        agent._end_session_on_close = False
        agent._print_fn = lambda *a, **kw: None
        compressed_head, _ = agent._compress_context(
            head,
            "",
            approx_tokens=estimate_message_tokens(head),
            focus_topic=FOCUS,
        )
        compressor = agent.context_compressor
        if bool(getattr(compressor, "_last_compress_aborted", False)):
            emit(status="summary_failed", session_id=args.session_id)
            return 5
        if not bool(getattr(compressor, "_last_compression_made_progress", False)):
            emit(status="no_progress", session_id=args.session_id)
            return 6
        compacted = rejoin_compressed_head_and_tail(compressed_head, tail)
        after_tokens = estimate_message_tokens(compacted)
        if len(compacted) >= len(history) or after_tokens >= before_tokens:
            emit(
                status="not_smaller",
                session_id=args.session_id,
                before_messages=len(history),
                after_messages=len(compacted),
                before_tokens=before_tokens,
                after_tokens=after_tokens,
            )
            return 7
        inserted = guarded_archive(db, args.session_id, before, compacted)
        if inserted is None:
            emit(status="changed", session_id=args.session_id)
            return 8
        emit(
            status="success",
            session_id=args.session_id,
            keep=args.keep,
            before_messages=len(history),
            after_messages=inserted,
            before_tokens=before_tokens,
            after_tokens=after_tokens,
            elapsed_seconds=round(time.monotonic() - started, 1),
        )
        return 0
    finally:
        db.release_compression_lock(args.session_id, holder)


if __name__ == "__main__":
    raise SystemExit(main())
