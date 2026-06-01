"""Core error types for Topos."""


class ToposError(Exception):
    """Base exception for Topos-specific errors."""


class ConfigurationError(ToposError):
    """Raised when configuration is invalid or missing."""


class NotReadyError(ToposError):
    """Raised when a service is not initialized."""
