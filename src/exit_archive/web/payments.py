"""Payment seam. T3.3 supplies the Stripe-backed verifier; here only the contract lives."""

from typing import Protocol

__all__ = ["AllowAllVerifier", "PaymentVerifier"]


class PaymentVerifier(Protocol):
    async def verify(self, session_id: str) -> bool:
        """Return True when ``session_id`` is a paid checkout session."""
        ...


class AllowAllVerifier:
    """Used only when ``PAYMENT_REQUIRED=false`` (development and tests)."""

    async def verify(self, session_id: str) -> bool:
        return True
