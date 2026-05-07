"""Concrete security-rule implementations."""

from .cost_budget_rule import CostBudgetRule
from .file_size_rule import FileSizeRule
from .llm_bash_review_rule import LlmBashReviewRule
from .llm_python_review_rule import LlmPythonReviewRule
from .llm_safe_bash_review_rule import LlmSafeBashReviewRule
from .llm_safe_python_review_rule import LlmSafePythonReviewRule
from .network_egress_rule import NetworkEgressRule
from .rate_limit_rule import RateLimitRule
from .secret_egress_rule import SecretEgressRule
from .subagent_depth_rule import SubagentDepthRule

# from .workspace_path_rule import WorkspacePathRule  # 【DISABLED】widget 迁移阶段暂停
from .tool_call_loop_detector_rule import ToolCallLoopDetectorRule
from .tool_confirm_rule import ToolConfirmRule

RULE_REGISTRY = {
    "llm_bash_review": LlmBashReviewRule,
    "llm_safe_bash_review": LlmSafeBashReviewRule,
    "llm_python_review": LlmPythonReviewRule,
    "llm_safe_python_review": LlmSafePythonReviewRule,
    # "workspace_path": WorkspacePathRule,  # 【DISABLED】widget 迁移阶段暂停
    "tool_call_loop_detector": ToolCallLoopDetectorRule,
    "rate_limit": RateLimitRule,
    "cost_budget": CostBudgetRule,
    "network_egress": NetworkEgressRule,
    "secret_egress": SecretEgressRule,
    "subagent_depth": SubagentDepthRule,
    "file_size": FileSizeRule,
    "tool_confirm": ToolConfirmRule,
}


__all__ = [
    "RULE_REGISTRY",
    "LlmBashReviewRule",
    "LlmSafeBashReviewRule",
    "LlmPythonReviewRule",
    "LlmSafePythonReviewRule",
    # "WorkspacePathRule",  # 【DISABLED】widget 迁移阶段暂停
    "ToolCallLoopDetectorRule",
    "RateLimitRule",
    "CostBudgetRule",
    "NetworkEgressRule",
    "SecretEgressRule",
    "SubagentDepthRule",
    "FileSizeRule",
    "ToolConfirmRule",
]
