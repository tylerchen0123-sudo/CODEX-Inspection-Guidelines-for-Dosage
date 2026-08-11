import unittest
from collector import _to_epoch_ms,calc_cost

class T(unittest.TestCase):
    def test_epoch(self):
        self.assertEqual(_to_epoch_ms(1700000000),170000000000)

    def test_cost(self):
        p={"default":{"input":1,"output":4}}
        self.assertEqual(calc_cost(p,"x",1000000,0,0,1000000),5)
