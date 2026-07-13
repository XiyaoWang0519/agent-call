from __future__ import annotations

import re
from dataclasses import dataclass, field

import phonenumbers
from phonenumbers import PhoneNumberFormat

from app.models import ContextPacket
from app.settings import Settings

N11_CODES = {f"{digit}11" for digit in range(2, 10)}
BLOCKED_SHORT_CODES = N11_CODES | {"911", "933", "988"}
SENSITIVE_PATTERNS = re.compile(
    r"\b(password|passcode|one[- ]time code|otp|auth(?:entication)? code|cvv|"
    r"credit card|debit card|card number|bank account|routing number|security code|"
    r"social security|ssn|tax(?:payer)? id|national id|government id|passport number|"
    r"driver'?s license|date of birth)\b",
    re.IGNORECASE,
)


@dataclass(slots=True)
class PolicyDecision:
    allowed: bool
    code: str = "ok"
    message: str = "allowed"
    details: dict[str, object] = field(default_factory=dict)


def validate_destination(phone: str, settings: Settings) -> PolicyDecision:
    digits = re.sub(r"\D", "", phone)
    national_tail = digits[-3:]
    if national_tail in BLOCKED_SHORT_CODES and len(digits) <= 4:
        return PolicyDecision(False, "blocked_short_code", "Emergency and N11 numbers are blocked")
    if digits.startswith("1900") or digits.startswith("900"):
        return PolicyDecision(False, "premium_rate", "Premium-rate destinations are blocked")
    try:
        parsed = phonenumbers.parse(phone, None)
    except phonenumbers.NumberParseException:
        return PolicyDecision(False, "invalid_e164", "Destination must be valid E.164")
    normalized = phonenumbers.format_number(parsed, PhoneNumberFormat.E164)
    if normalized != phone or not phonenumbers.is_possible_number(parsed):
        return PolicyDecision(False, "invalid_e164", "Destination must be valid E.164")
    if not any(phone.startswith(prefix) for prefix in settings.allowed_country_codes):
        return PolicyDecision(
            False,
            "country_not_allowed",
            "Destination country code is not allowed",
            {"allowed_country_codes": settings.allowed_country_codes},
        )
    if settings.twilio_caller_id and phone == settings.twilio_caller_id:
        return PolicyDecision(False, "service_number", "The service cannot call its own number")
    return PolicyDecision(True)


def validate_context(packet: ContextPacket, settings: Settings) -> list[PolicyDecision]:
    errors: list[PolicyDecision] = []
    destination = validate_destination(packet.target.phone, settings)
    if not destination.allowed:
        errors.append(destination)
    if packet.escalation.owner_phone != packet.owner.callback_number:
        errors.append(
            PolicyDecision(
                False,
                "owner_phone_mismatch",
                "Escalation owner phone must match the owner's callback number",
            )
        )
    if settings.owner_phone_e164 and packet.owner.callback_number != settings.owner_phone_e164:
        errors.append(
            PolicyDecision(
                False,
                "configured_owner_phone_mismatch",
                "Owner callback number must match configured OWNER_PHONE_E164",
            )
        )
    if packet.owner.display_name != "Irvin":
        errors.append(
            PolicyDecision(
                False,
                "owner_identity_mismatch",
                "This single-user service is configured only for owner Irvin",
            )
        )
    text_fields = [
        packet.objective,
        *packet.relevant_facts,
        *packet.preferences,
        *packet.hard_constraints,
        *packet.allowed_commitments,
        *packet.prohibited_actions,
    ]
    if any(SENSITIVE_PATTERNS.search(value) for value in text_fields):
        errors.append(
            PolicyDecision(
                False,
                "sensitive_data",
                "Context must not include passwords, auth codes, payment credentials, or government identifiers",
            )
        )
    return errors
