"""Errors shared by the REST and ASCEND cloud clients."""


class IntermaticError(Exception):
    """Base Intermatic error."""


class IntermaticAuthError(IntermaticError):
    """Authentication failed."""


class IntermaticConnectionError(IntermaticError):
    """Cloud connection failed."""
