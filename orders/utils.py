from django.conf import settings

NUMBER_PREFIX = "DD"


def _extract_number(value):
    if value is None:
        return None
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    if not digits:
        return None
    return int(digits)


def format_order_id(value):
    number = _extract_number(value)
    if number is None:
        return None
    return f"{NUMBER_PREFIX}{number:03d}"


def format_bill_number(value):
    number = _extract_number(value)
    if number is None:
        return None
    return f"{NUMBER_PREFIX}{number:04d}"



