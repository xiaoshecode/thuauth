"""Application errors safe to display without including configuration values."""


class ThuauthError(Exception):
    """Base class for expected application failures."""


class ConfigError(ThuauthError):
    """A field-specific configuration error; messages must not include input values."""

    def __init__(self, field: str, message: str) -> None:
        self.field = field
        self.message = message
        super().__init__(f"{field}: {message}")


class ProtocolError(ThuauthError):
    """Malformed local input or untrusted response, without raw protocol material."""

    _MESSAGES = {
        "invalid_input": "协议输入无效",
        "invalid_callback": "JSONP 回调名称无效或不匹配",
        "response_too_large": "认证响应超过大小限制",
        "invalid_jsonp": "认证响应不是有效的 JSONP 对象",
        "invalid_response": "认证响应缺少必要字段或字段无效",
    }

    def __init__(self, code: str) -> None:
        self.code = code if code in self._MESSAGES else "invalid_response"
        super().__init__(self._MESSAGES[self.code])
