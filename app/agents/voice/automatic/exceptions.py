"""
Custom exceptions for voice agent function confirmation system.
These exceptions signal to the LLM framework that operations should NOT be retried.
"""

class UserRejectedOperationError(Exception):
    """
    Raised when user explicitly rejects a dangerous operation.
    This signals to the LLM that the operation should not be retried.
    """
    pass

class OperationTimeoutError(Exception):
    """
    Raised when operation times out waiting for user response.
    This signals to the LLM that the operation should not be retried.
    """
    pass

class ConfirmationError(Exception):
    """
    Raised when confirmation process fails due to system errors.
    This signals to the LLM that the operation should not be retried.
    """
    pass
