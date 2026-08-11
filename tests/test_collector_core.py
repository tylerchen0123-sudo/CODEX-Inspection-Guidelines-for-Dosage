import unittest

from collector import Call, _to_epoch_ms, calc_cost


class CollectorCoreTests(unittest.TestCase):
    def test_epoch_seconds_to_ms(self):
        self.assertEqual(_to_epoch_ms(1_700_000_000), 1_700_000_000_000)

    def test_cost_per_million(self):
        pricing = {"models": {}, "default": {"input": 1, "cache_read": 0, "cache_write": 0, "output": 4}}
        self.assertEqual(calc_cost(pricing, "unknown", 1_000_000, 0, 0, 1_000_000), 5.0)

    def test_negative_tokens_are_clamped(self):
        call = Call("codex", 1, "model", "session", "project", -1, 2, -3, 4, -5)
        self.assertEqual((call.fresh_in, call.cache_read, call.cache_write,
