"""
Logging filters — redact secrets that would otherwise leak into stdout/journald.

uvicorn's WS handshake lifecycle logging (accept/reject/http-response,
in uvicorn.protocols.websockets.*) logs the raw request path including
query string at INFO level on every connection. This app's WS auth is a
query-string JWT (?token=...), so those lines put live, valid bearer
tokens in plaintext into journald on every connect — confirmed live in
production. This filter redacts the token value before the record is
formatted, without touching real error/warning logging on the same
logger.
"""
import re

_TOKEN_RE = re.compile(r"([?&]token=)[^&\s\"]+")


class RedactTokenFilter:
    def filter(self, record):
        if record.args:
            record.args = tuple(
                _TOKEN_RE.sub(r"\1<redacted>", arg) if isinstance(arg, str) else arg
                for arg in record.args
            )
        return True
