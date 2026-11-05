class WattMagentError(Exception):
    """Base class for all application errors."""


class LLMError(WattMagentError):
    """Generic LLM provider failure."""


class CreditsExhausted(LLMError):
    """Provider reports no remaining credits (HTTP 402). Retrying other models won't help."""


class AuthError(LLMError):
    """Token missing or rejected (HTTP 401/403)."""


class ModelUnavailable(LLMError):
    """This model cannot serve the request; try the next one in the chain."""


class ProviderUnavailable(LLMError):
    """Every model in the fallback chain failed."""


class BudgetExceeded(WattMagentError):
    """Our own daily token budget is used up (protects the free tier)."""


class ReplanError(WattMagentError):
    """A replan request that cannot be carried out; ``status_code`` is the HTTP meaning."""

    def __init__(self, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code
