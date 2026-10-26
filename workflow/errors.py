"""Errors whose public messages are safe to return from the HTTP API."""


class BrokerError(Exception):
    """Base class for expected broker failures."""

    status_code = 500
    code = "internal_error"

    def __init__(self, message: str = "internal broker error") -> None:
        super().__init__(message)
        self.public_message = message


class BadRequest(BrokerError):
    status_code = 400
    code = "bad_request"


class Unauthorized(BrokerError):
    status_code = 401
    code = "unauthorized"


class Forbidden(BrokerError):
    status_code = 403
    code = "forbidden"


class NotFound(BrokerError):
    status_code = 404
    code = "not_found"


class Conflict(BrokerError):
    status_code = 409
    code = "conflict"


class MethodNotAllowed(BrokerError):
    status_code = 405
    code = "method_not_allowed"


class PayloadTooLarge(BrokerError):
    status_code = 413
    code = "payload_too_large"


class UnsupportedMediaType(BrokerError):
    status_code = 415
    code = "unsupported_media_type"


class CapabilityExpired(BrokerError):
    status_code = 410
    code = "capability_expired"


class ServiceUnavailable(BrokerError):
    status_code = 503
    code = "service_unavailable"
