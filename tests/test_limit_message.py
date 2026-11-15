import unittest
from loop.adapters import parse


class LimitMessage(unittest.TestCase):
    def test_claude_five_hour_limit_is_rate_limited(self):
        out = '{"type":"result","is_error":true,"result":"You\'ve hit your 5-hour limit · resets 1:20pm"}'
        self.assertEqual(parse("claude", 1, out, "").status, "rate_limited")


if __name__ == "__main__":
    unittest.main()
