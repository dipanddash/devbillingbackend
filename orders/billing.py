from decimal import Decimal, InvalidOperation


MONEY_QUANT = Decimal("0.01")


def to_decimal(value, default="0"):
    try:
        if value in (None, ""):
            return Decimal(str(default))
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(str(default))


def quantize_money(value):
    amount = to_decimal(value, default="0")
    return amount.quantize(MONEY_QUANT)


def parse_positive_quantity(value):
    if isinstance(value, bool):
        raise ValueError("Quantity must be a valid integer")

    if isinstance(value, int):
        qty = value
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            raise ValueError("Quantity must be a valid integer")
        qty = int(raw)
    else:
        raise ValueError("Quantity must be a valid integer")

    if qty <= 0:
        raise ValueError("Quantity must be greater than 0")
    return qty


def parse_non_negative_amount(value):
    if value in (None, ""):
        raise ValueError("Amount is required")
    try:
        amount = Decimal(str(value).strip())
    except Exception as exc:  # noqa: BLE001
        raise ValueError("Amount must be numeric") from exc
    if amount < 0:
        raise ValueError("Amount cannot be negative")
    return quantize_money(amount)


def normalize_phone(phone):
    if phone in (None, ""):
        return ""
    digits = "".join(ch for ch in str(phone) if ch.isdigit())
    if len(digits) < 10:
        return ""
    return digits[-10:]


def calculate_line_amounts(menu_price, gst_percent, addon_total=Decimal("0.00")):
    menu = quantize_money(menu_price)
    addon = quantize_money(addon_total)
    percent = to_decimal(gst_percent, default="0")

    # Tax is applied on base menu price. Addons are tax-neutral for now.
    unit_tax = quantize_money((menu * percent) / Decimal("100"))
    base_price = quantize_money(menu + addon)
    unit_total = quantize_money(base_price + unit_tax)
    return {
        "menu_price": menu,
        "addon_total": addon,
        "base_price": base_price,
        "gst_percent": percent,
        "gst_amount": unit_tax,
        "unit_total": unit_total,
    }


def calculate_payable_amount(subtotal, discount):
    subtotal_money = quantize_money(subtotal)
    discount_money = quantize_money(discount)
    return quantize_money(max(subtotal_money - discount_money, Decimal("0.00")))
