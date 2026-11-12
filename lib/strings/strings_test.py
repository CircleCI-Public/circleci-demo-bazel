import unittest
from lib.strings.strings import capitalize_words, truncate, repeat


class StringsTest(unittest.TestCase):
    def test_capitalize_words(self):
        self.assertEqual(capitalize_words("hello world"), "Hello World")

    def test_truncate_long(self):
        self.assertEqual(truncate("hello world", 5), "hello...")

    def test_truncate_short(self):
        self.assertEqual(truncate("hi", 10), "hi")

    def test_repeat(self):
        self.assertEqual(repeat("ab", 3), "ababab")


if __name__ == "__main__":
    unittest.main()
