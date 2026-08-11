import unittest

from collector import Call, _to_epoch_ms, calc_cost, price_of


class CollectorUtilityTests(unittest.TestCase):
    def test_to_epoch_ms_accepts_seconds_and_milliseconds(self):
        self.assertEqual(_to_epoch_ms(1_700_000_000), 1_700_000_000_000)
      
