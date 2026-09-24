"""Application errors independent of the HTTP and Telegram transports."""


class DomainError(Exception):
    """An expected business-rule failure with a message safe for the caller."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class InvalidInput(DomainError):
    """Input is syntactically valid but violates a business rule."""


class EntityNotFound(DomainError):
    """The requested entity does not exist or is not visible to the caller."""


class AccessDenied(DomainError):
    """The caller does not have permission for the operation."""


class StateConflict(DomainError):
    """The operation conflicts with the entity's current state."""
