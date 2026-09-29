# The loop only looks at `disposition`, never at the exception type.

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar


class Disposition(StrEnum):
    RETRYABLE = "retryable"
    RECOVERABLE = "recoverable"
    FATAL = "fatal"


class LegionError(Exception):
    code: ClassVar[str] = "legion_error"
    disposition: ClassVar[Disposition] = Disposition.FATAL

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


# config / startup


class ConfigError(LegionError):
    code = "config_error"


class InvalidTransition(LegionError):
    code = "invalid_transition"


class NoModelBinding(ConfigError):
    code = "no_model_binding"


# model calls


class ModelTimeout(LegionError):
    code = "model_timeout"
    disposition = Disposition.RETRYABLE


class ModelUnavailable(LegionError):
    code = "model_unavailable"
    disposition = Disposition.RETRYABLE


class RateLimited(LegionError):
    code = "rate_limited"
    disposition = Disposition.RETRYABLE

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class MalformedModelResponse(LegionError):
    code = "malformed_model_response"
    disposition = Disposition.RETRYABLE


class ModelAuthError(LegionError):
    code = "model_auth_error"


class ModelRequestRejected(LegionError):
    code = "model_request_rejected"


class ContextExhausted(LegionError):
    code = "context_exhausted"


# refused before running; the model gets told and can try something else


class ActionRefused(LegionError):
    disposition = Disposition.RECOVERABLE


class UnknownTool(ActionRefused):
    code = "unknown_tool"


class ToolNotOffered(ActionRefused):
    code = "tool_not_offered"


class InvalidArguments(ActionRefused):
    code = "invalid_arguments"


class CapabilityDenied(ActionRefused):
    code = "capability_denied"


class PolicyDenied(ActionRefused):
    code = "policy_denied"


class GrantExpired(LegionError):
    code = "grant_expired"


class RepeatedAction(ActionRefused):
    code = "repeated_action"


class LoopDetected(LegionError):
    code = "loop_detected"


# tool execution


class ToolTimeout(LegionError):
    code = "tool_timeout"
    disposition = Disposition.RETRYABLE


class ToolFailed(LegionError):
    code = "tool_failed"
    disposition = Disposition.RECOVERABLE


# tools raise this for transient failures (e.g. a 503 upstream)
class ToolRetryable(LegionError):
    code = "tool_retryable"
    disposition = Disposition.RETRYABLE


class InvalidToolOutput(LegionError):
    code = "invalid_tool_output"
    disposition = Disposition.RECOVERABLE


# a write may or may not have happened
class ActionInDoubt(LegionError):
    code = "action_in_doubt"


class CredentialUnavailable(LegionError):
    code = "credential_unavailable"


# run limits


class BudgetExceeded(LegionError):
    code = "budget_exceeded"

    def __init__(self, dimension: str, limit: object, attempted: object) -> None:
        super().__init__(f"budget exceeded: {dimension} limit {limit}, would reach {attempted}")
        self.dimension = dimension
        self.limit = limit
        self.attempted = attempted


class Killed(LegionError):
    code = "killed"


class DeadlineExceeded(LegionError):
    code = "deadline_exceeded"


class FinalOutputInvalid(LegionError):
    code = "final_output_invalid"


class ApprovalUnavailable(ActionRefused):
    code = "approval_unavailable"
