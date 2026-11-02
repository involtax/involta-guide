class ProviderUnavailable(RuntimeError):
    retryable = True


class GenerationStopped(ProviderUnavailable):
    """A new paid POST must never be an automatic retry of this operation."""

    retryable = False


class GenerationPending(Exception):
    """Release the worker; poll the existing response on the next scheduled visit."""

    def __init__(self, message: str = "") -> None:
        super().__init__(message)
