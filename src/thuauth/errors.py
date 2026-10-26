"""Application errors safe to display without including configuration values."""


class ThuauthError(Exception):
    """Base class for expected application failures."""


class ConfigError(ThuauthError):
    """A field-specific configuration error; messages must not include input values."""

    def __init__(self, field: str, message: str) -> None:
        self.field = field
        self.message = message
        super().__init__(f"{field}: {message}")
