"""Exception hierarchy shared across the package."""

from __future__ import annotations


class OddsScannerError(Exception):
    """Base class for all errors raised by this package."""


class ConfigError(OddsScannerError):
    """Invalid or missing configuration (including missing environment variables)."""


class ProviderError(OddsScannerError):
    """An odds provider failed to return usable data."""


class AuthenticationError(ProviderError):
    """The provider rejected our credentials. Retrying will not help."""


class QuotaExhaustedError(ProviderError):
    """The provider's request quota is used up. Retrying will not help."""


class QuotaLowError(ProviderError):
    """We chose to stop polling because the remaining request quota is below the safety floor."""


class RateLimitError(ProviderError):
    """The provider throttled us (HTTP 429). Wait ``retry_after`` seconds, if known."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class NotificationError(OddsScannerError):
    """A notification could not be delivered."""


class BlockedError(ProviderError):
    """The site refused us (HTTP 403, captcha or bot-check page). We stop polling it - never try to bypass it."""
