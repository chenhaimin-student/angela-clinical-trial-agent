from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from angela_orchestration.request_state import (
    InvalidTransition,
    RequestState,
    RequestStateStore,
)


class RequestStateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "runtime.sqlite3"
        self.store = RequestStateStore(self.path)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def create(self, request_id="req-1", key="message-1"):
        return self.store.create_or_get(
            request_id=request_id,
            idempotency_key=key,
            session_key="wecom:chat:user",
            channel="wecom",
            payload={"text": "SLE有什么项目"},
        )

    def test_duplicate_message_reuses_original_request(self) -> None:
        first, created = self.create()
        duplicate, duplicate_created = self.create(request_id="req-2")
        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertEqual(duplicate.request_id, first.request_id)

    def test_valid_transitions_are_persisted(self) -> None:
        record, _ = self.create()
        self.store.transition(record.request_id, RequestState.PARSING, stage="patient_fact_extraction")
        self.store.transition(record.request_id, RequestState.READY, stage="route_decision")
        self.store.transition(record.request_id, RequestState.QUEUED, queue_position=2)
        running = self.store.transition(record.request_id, RequestState.RUNNING, stage="local_query")
        self.assertEqual(running.state, RequestState.RUNNING)
        reopened = RequestStateStore(self.path)
        self.assertEqual(reopened.get(record.request_id).state, RequestState.RUNNING)

    def test_invalid_transition_is_rejected(self) -> None:
        record, _ = self.create()
        with self.assertRaises(InvalidTransition):
            self.store.transition(record.request_id, RequestState.COMPLETED)

    def test_recoverable_requests_survive_store_restart(self) -> None:
        record, _ = self.create()
        self.store.transition(record.request_id, RequestState.PARSING)
        self.store.transition(record.request_id, RequestState.READY)
        self.store.transition(record.request_id, RequestState.QUEUED, queue_position=1)
        reopened = RequestStateStore(self.path)
        self.assertEqual([item.request_id for item in reopened.recoverable()], [record.request_id])

    def test_interrupted_request_is_replayed_once_with_same_identity(self) -> None:
        record, _ = self.create()
        self.store.transition(record.request_id, RequestState.PARSING)
        self.store.transition(record.request_id, RequestState.READY)
        self.store.transition(record.request_id, RequestState.QUEUED, queue_position=3)
        self.store.transition(record.request_id, RequestState.RUNNING)

        reopened = RequestStateStore(self.path)
        self.assertEqual(reopened.prepare_interrupted_for_replay(), 1)
        pending = reopened.get(record.request_id)
        self.assertEqual(pending.state, RequestState.READY)
        self.assertEqual(pending.reason_code, "PROCESS_RESTART_RECOVERY_PENDING")

        claimed, did_claim = reopened.claim_replayed("message-1")
        self.assertTrue(did_claim)
        self.assertEqual(claimed.request_id, record.request_id)
        self.assertEqual(claimed.state, RequestState.PARSING)

        duplicate, second_claim = reopened.claim_replayed("message-1")
        self.assertFalse(second_claim)
        self.assertEqual(duplicate.request_id, record.request_id)

    def test_only_not_started_request_can_be_cancelled(self) -> None:
        record, _ = self.create()
        self.store.transition(record.request_id, RequestState.PARSING)
        self.store.transition(record.request_id, RequestState.READY)
        self.store.transition(record.request_id, RequestState.QUEUED)
        cancelled = self.store.cancel_if_queued(record.request_id)
        self.assertEqual(cancelled.state, RequestState.CANCELLED)

    def test_queue_position_is_persisted_only_while_queued(self) -> None:
        record, _ = self.create()
        self.store.transition(record.request_id, RequestState.PARSING)
        self.store.transition(record.request_id, RequestState.READY)
        self.store.transition(record.request_id, RequestState.QUEUED)
        updated = self.store.update_queue_position(record.request_id, 4)
        self.assertEqual(updated.queue_position, 4)
        self.store.transition(record.request_id, RequestState.RUNNING)
        with self.assertRaises(InvalidTransition):
            self.store.update_queue_position(record.request_id, 2)


if __name__ == "__main__":
    unittest.main()
