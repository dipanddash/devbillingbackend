from django.test import SimpleTestCase

from .views import _parse_positive_quantity


class QuantityParsingTests(SimpleTestCase):
    def test_accepts_positive_integer_and_numeric_string(self):
        self.assertEqual(_parse_positive_quantity(5), 5)
        self.assertEqual(_parse_positive_quantity("5"), 5)
        self.assertEqual(_parse_positive_quantity(" 7 "), 7)

    def test_rejects_bool(self):
        with self.assertRaises(ValueError):
            _parse_positive_quantity(True)

    def test_rejects_zero_negative_empty_and_decimal_string(self):
        invalid_values = [0, -1, "", "   ", "5.5", "-2"]
        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    _parse_positive_quantity(value)
