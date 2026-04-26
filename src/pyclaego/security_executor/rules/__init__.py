"""Concrete security-rule implementations."""

from .llm_bash_review_rule import LlmBashReviewRule
from .llm_safe_bash_review_rule import LlmSafeBashReviewRule
from .workspace_path_rule import WorkspacePathRule
from .tool_call_loop_detector_rule import ToolCallLoopDetectorRule
from .rate_limit_rule import RateLimitRule
from .cost_budget_rule import CostBudgetRule
from .network_egress_rule import NetworkEgressRule
from .secret_egress_rule import SecretEgressRule
from .subagent_depth_rule import SubagentDepthRule
from .file_size_rule import FileSizeRule


RULE_REGISTRY = {
    "llm_bash_review": LlmBashReviewRule,
    "llm_safe_bash_review": LlmSafeBashReviewRule,
    "workspace_path": WorkspacePathRule,
    "tool_call_loop_detector": ToolCallLoopDetectorRule,
    "rate_limit": RateLimitRule,
    "cost_budget": CostBudgetRule,
    "network_egress": NetworkEgressRule,
    "secret_egress": SecretEgressRule,
    "subagent_depth": SubagentDepthRule,
    "file_size": FileSizeRule,
}


__all__ = [
    "RULE_REGISTRY",
    "LlmBashReviewRule",
    "LlmSafeBashReviewRule",
    "WorkspacePathRule",
    "ToolCallLoopDetectorRule",
    "RateLimitRule",
    "CostBudgetRule",
    "NetworkEgressRule",
    "SecretEgressRule",
    "SubagentDepthRule",
    "FileSizeRule",
]
