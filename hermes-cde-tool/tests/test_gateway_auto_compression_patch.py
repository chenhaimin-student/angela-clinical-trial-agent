from pathlib import Path
import unittest


class GatewayAutoCompressionPatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (
            Path(__file__).resolve().parents[1]
            / "patches"
            / "gateway_auto_compression.patch"
        ).read_text(encoding="utf-8")

    def test_absolute_token_limit_is_configurable(self):
        self.assertIn('gateway_hygiene_token_limit', self.source)
        self.assertIn('_compress_token_threshold = min(', self.source)
        self.assertIn('_hyg_absolute_token_limit,', self.source)

    def test_recent_exchange_tail_is_preserved(self):
        self.assertIn('gateway_hygiene_keep_recent_exchanges', self.source)
        self.assertIn('split_history_for_partial_compress', self.source)
        self.assertIn('rejoin_compressed_head_and_tail', self.source)

    def test_partial_result_is_soft_archived_only_after_progress(self):
        self.assertIn('_partial_progress', self.source)
        self.assertIn('_hyg_session_db.archive_and_compact(', self.source)
        self.assertIn('_hyg_agent._last_compaction_in_place = True', self.source)

    def test_summary_failure_preserves_original_history(self):
        self.assertIn('_partial_aborted', self.source)
        self.assertIn('and not _partial_aborted', self.source)


if __name__ == "__main__":
    unittest.main()
