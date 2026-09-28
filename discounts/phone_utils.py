"""Pakistani mobile number validation and E.164 normalization."""

from __future__ import annotations

import re

from rest_framework.exceptions import ValidationError

# International: +92 3XXXXXXXXX / 0092… / local 03XXXXXXXXX
_E164_PK_MOBILE = re.compile(r"^\+923\d{9}$")


def normalize_pakistani_mobile(value: str | None) -> str:
    """
    Accept 03XX-XXXXXXX / 03XXXXXXXXX / +92 3XX XXXXXXX and return +923XXXXXXXXX.
    Raises ValidationError when the value is not a Pakistani mobile number.
    """
    raw = (value or "").strip()
    if not raw:
        raise ValidationError({"customer_phone": "Mobile number is required."})

    digits = re.sub(r"\D", "", raw)
    if digits.startswith("0092"):
        digits = digits[2:]  # -> 92…
    if digits.startswith("92") and len(digits) == 12:
        normalized = f"+{digits}"
    elif digits.startswith("0") and len(digits) == 11:
        normalized = f"+92{digits[1:]}"
    elif len(digits) == 10 and digits.startswith("3"):
        normalized = f"+92{digits}"
    else:
        raise ValidationError(
            {
                "customer_phone": (
                    "Enter a Pakistani mobile number "
                    "(03XX-XXXXXXX or +92 3XX XXXXXXX)."
                )
            }
        )

    if not _E164_PK_MOBILE.match(normalized):
        raise ValidationError(
            {
                "customer_phone": (
                    "Enter a Pakistani mobile number "
                    "(03XX-XXXXXXX or +92 3XX XXXXXXX)."
                )
            }
        )
    return normalized
