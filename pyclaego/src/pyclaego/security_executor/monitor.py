"""Security module — audits LLM calls and tool calls against configured rules."""

import traceback
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import PYCLAEGO_DEFAULT_LOGS_ROOT, get_config
from ..logging import get_running_log
from .auditor import SecurityAuditor
from .base_rule import BaseSecurityRule, RequestType, SecurityDecision
from .rule_factory import SecurityRuleFactory

_rlog = get_running_log()


class SecurityMonitor:
    """Security monitor — orchestrates rule evaluation.

    Responsibilities:
    - Receive LLM / tool-call requests and run rule evaluation
    - Compute the final decision and forward it to stateful rules via
      ``on_request_completed``
    - Track global + per-rule statistics and delegate event logging to
      ``SecurityAuditor``.
    """

    def __init__(self):
        config = get_config()
        self.security_config = config.get("security", {})
        self.enabled = self.security_config.get("enabled", False)
        log_enabled = self.security_config.get("log_enabled", True)

        # Event log root directory
        session_config = config.get("session", {})
        log_root = session_config.get("log_root", PYCLAEGO_DEFAULT_LOGS_ROOT)
        self.log_root = Path(log_root).expanduser()

        # Event buffer size (bounds in-memory growth)
        event_buffer_size = int(self.security_config.get("event_buffer_size", 1000))

        self.auditor = SecurityAuditor(
            log_root=self.log_root,
            log_enabled=log_enabled,
            event_buffer_size=event_buffer_size,
        )

        # Load rules
        self.rules: list[BaseSecurityRule] = []
        rules_config = self.security_config.get("rules", [])
        if rules_config:
            _rlog.info("core_service", "[SecurityMonitor] Loading security rules...")
            self.rules = SecurityRuleFactory.create_rules_from_config(rules_config)
            _rlog.info(
                "core_service",
                f"[SecurityMonitor] Loaded {len(self.rules)} security rule(s)",
            )
        else:
            _rlog.info("core_service", "[SecurityMonitor] No security rules configured")

        # Global stats
        self.stats: dict[str, int] = {
            "total_requests": 0,
            "allowed": 0,
            "denied": 0,
            "warned": 0,
        }

        # Per-rule stats
        self.rule_stats: dict[str, dict[str, int]] = defaultdict(
            lambda: {"matched": 0, "allowed": 0, "denied": 0, "warned": 0, "errors": 0}
        )

        _rlog.info(
            "core_service",
            f"[SecurityMonitor] Initialized (enabled={self.enabled}, rules={len(self.rules)})",
        )

    # -----------------------------------------------------------
    # Core evaluation
    # -----------------------------------------------------------

    async def _evaluate_rules(
        self,
        request_type: RequestType,
        request_dict: dict[str, Any],
    ) -> dict[str, Any]:
        """Walk rules, compute decision, update stats, emit event."""
        self.stats["total_requests"] += 1

        if not self.enabled:
            self.stats["allowed"] += 1
            return {
                "decision": SecurityDecision.ALLOW.value,
                "reason": "Security check disabled",
                "matched_rules": [],
            }

        matched_rules: list[str] = []
        matched_reasons: list[str] = []
        final_decision = SecurityDecision.ALLOW
        final_query_spec: dict[str, Any] | None = None  # set when QUERY wins

        for rule in self.rules:
            try:
                hit = await rule.matches(request_dict)
            except Exception as e:
                self.rule_stats[rule.rule_id]["errors"] += 1
                _rlog.error(
                    "core_service",
                    f"[SecurityMonitor] Rule {rule.rule_id} matches() raised: {e}\n{traceback.format_exc()}",
                )
                continue

            if not hit:
                continue

            matched_rules.append(rule.rule_id)
            self.rule_stats[rule.rule_id]["matched"] += 1
            try:
                matched_reasons.append(f"[{rule.rule_id}] {rule.get_match_reason()}")
            except Exception:
                matched_reasons.append(f"[{rule.rule_id}] (reason unavailable)")

            decision = rule.get_decision()
            if decision == SecurityDecision.DENY:
                self.rule_stats[rule.rule_id]["denied"] += 1
                final_decision = SecurityDecision.DENY
                break
            elif decision == SecurityDecision.QUERY:
                self.rule_stats[rule.rule_id]["warned"] += 1
                if final_decision not in (SecurityDecision.DENY,):
                    final_decision = SecurityDecision.QUERY
                    if final_query_spec is None:
                        try:
                            final_query_spec = rule.get_query_spec()
                        except Exception:
                            pass
            elif decision == SecurityDecision.WARN:
                self.rule_stats[rule.rule_id]["warned"] += 1
                if final_decision not in (SecurityDecision.DENY, SecurityDecision.QUERY):
                    final_decision = SecurityDecision.WARN
            else:
                self.rule_stats[rule.rule_id]["allowed"] += 1

        # Global stats
        if final_decision == SecurityDecision.ALLOW:
            self.stats["allowed"] += 1
        elif final_decision == SecurityDecision.DENY:
            self.stats["denied"] += 1
        else:
            self.stats["warned"] += 1

        result: dict[str, Any] = {
            "decision": final_decision.value,
            "reason": " | ".join(matched_reasons) if matched_reasons else "No rules matched",
            "matched_rules": matched_rules,
        }
        if final_query_spec is not None:
            result["query_spec"] = final_query_spec

        # Notify stateful rules (before-phase completion)
        await self._notify_completed(
            request_dict,
            {
                "hook": "before",
                "decision": final_decision.value,
                "matched_rules": matched_rules,
            },
        )

        # Log event
        event = {
            "hook": "before",
            "timestamp": request_dict.get("timestamp", datetime.now().isoformat()),
            "session_id": request_dict.get("session_id", ""),
            "subagent_id": request_dict.get("subagent_id"),
            "request_type": request_type.value,
            "decision": final_decision.value,
            "matched_rules": matched_rules,
            "reason": result["reason"],
        }
        await self.auditor.log(event)

        return result

    async def _notify_completed(
        self,
        request_dict: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        """Notify every rule that a request has been processed, so stateful
        rules can update their counters."""
        for rule in self.rules:
            try:
                await rule.on_request_completed(request_dict, result)
            except Exception as e:
                _rlog.error(
                    "core_service",
                    f"[SecurityMonitor] Rule {rule.rule_id} on_request_completed raised: {e}",
                )

    # -----------------------------------------------------------
    # Before hooks
    # -----------------------------------------------------------

    async def before_tool_call(
        self,
        session_id: str,
        subagent_id: str | None,
        tool_name: str,
        tool_args: dict[str, Any],
    ) -> dict[str, Any]:
        request_dict = {
            "type": RequestType.TOOL_CALL.value,
            "session_id": session_id,
            "subagent_id": subagent_id,
            "timestamp": datetime.now().isoformat(),
            "tool_name": tool_name,
            "tool_args": tool_args,
        }
        return await self._evaluate_rules(RequestType.TOOL_CALL, request_dict)

    async def before_llm_call(
        self,
        session_id: str,
        subagent_id: str | None,
        llm_id: str,
        system: str | None,
        messages: list[Any],
        tool_list: list[Any] | None,
        tool_choices: str | None,
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        request_dict = {
            "type": RequestType.LLM_CALL.value,
            "session_id": session_id,
            "subagent_id": subagent_id,
            "timestamp": datetime.now().isoformat(),
            "llm_id": llm_id,
            "system_len": len(system) if system else 0,
            "message_count": len(messages),
            "tool_names": [t.name for t in tool_list] if tool_list else [],
            "tool_choices": tool_choices,
            "kwargs_keys": list(kwargs.keys()),
        }
        return await self._evaluate_rules(RequestType.LLM_CALL, request_dict)

    # -----------------------------------------------------------
    # After hooks (audit + notify rules)
    # -----------------------------------------------------------

    async def after_tool_call(
        self,
        session_id: str,
        subagent_id: str | None,
        tool_name: str,
        tool_args: dict[str, Any],
        success: bool,
        output: str | None,
        error: str | None,
    ) -> None:
        try:
            request_dict = {
                "type": RequestType.TOOL_CALL.value,
                "session_id": session_id,
                "subagent_id": subagent_id,
                "tool_name": tool_name,
                "tool_args": tool_args,
            }
            await self._notify_completed(
                request_dict,
                {
                    "hook": "after_tool_call",
                    "success": success,
                    "error": error,
                },
            )

            event = {
                "hook": "after_tool_call",
                "timestamp": datetime.now().isoformat(),
                "session_id": session_id,
                "subagent_id": subagent_id,
                "tool_name": tool_name,
                "success": success,
                "error": error,
            }
            await self.auditor.log(event)
        except Exception as e:
            _rlog.error(
                "core_service",
                f"[SecurityMonitor] after_tool_call logging failed: {e}\n{traceback.format_exc()}",
            )

    async def after_llm_call(
        self,
        session_id: str,
        subagent_id: str | None,
        llm_id: str,
        system: str | None,
        messages: list[Any],
        tool_list: list[Any] | None,
        tool_choices: str | None,
        kwargs: dict[str, Any],
        success: bool,
        stop_reason: str | None,
        error: str | None,
        usage: dict[str, Any] | None = None,
    ) -> None:
        """Post-LLM-call audit.

        New arg:
            usage: Token usage dict returned by the LLM provider. Forwarded to
                stateful rules (e.g. cost_budget). Defaults to ``None`` for
                backward compatibility.
        """
        try:
            request_dict = {
                "type": RequestType.LLM_CALL.value,
                "session_id": session_id,
                "subagent_id": subagent_id,
                "llm_id": llm_id,
                "message_count": len(messages),
            }
            await self._notify_completed(
                request_dict,
                {
                    "hook": "after_llm_call",
                    "success": success,
                    "stop_reason": stop_reason,
                    "error": error,
                    "usage": usage or {},
                },
            )

            event = {
                "hook": "after_llm_call",
                "timestamp": datetime.now().isoformat(),
                "session_id": session_id,
                "subagent_id": subagent_id,
                "llm_id": llm_id,
                "message_count": len(messages),
                "success": success,
                "stop_reason": stop_reason,
                "error": error,
                "usage": usage or {},
            }
            await self.auditor.log(event)
        except Exception as e:
            _rlog.error(
                "core_service",
                f"[SecurityMonitor] after_llm_call logging failed: {e}\n{traceback.format_exc()}",
            )

    # -----------------------------------------------------------
    # Backward-compat shim (legacy code path)
    # -----------------------------------------------------------

    async def review_request(
        self,
        request_type: RequestType,
        request_data: dict[str, Any],
        session_id: str,
    ) -> dict[str, Any]:
        subagent_id = request_data.get("subagent_id")
        if request_type == RequestType.TOOL_CALL:
            return await self.before_tool_call(
                session_id=session_id,
                subagent_id=subagent_id,
                tool_name=request_data.get("tool_name", ""),
                tool_args=request_data.get("tool_args", {}),
            )
        elif request_type == RequestType.LLM_CALL:
            return await self.before_llm_call(
                session_id=session_id,
                subagent_id=subagent_id,
                llm_id=request_data.get("llm_id", ""),
                system=request_data.get("system"),
                messages=request_data.get("messages", []),
                tool_list=request_data.get("tool_list"),
                tool_choices=request_data.get("tool_choices"),
                kwargs=request_data.get("kwargs", {}),
            )
        request_dict = {
            "type": request_type.value,
            "session_id": session_id,
            "subagent_id": subagent_id,
            "timestamp": datetime.now().isoformat(),
            **request_data,
        }
        return await self._evaluate_rules(request_type, request_dict)

    # -----------------------------------------------------------
    # Public accessors
    # -----------------------------------------------------------

    @property
    def events(self):
        """Backward-compat: expose the in-memory event deque."""
        return self.auditor.events

    def get_stats(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "total_requests": self.stats["total_requests"],
            "allowed": self.stats["allowed"],
            "denied": self.stats["denied"],
            "warned": self.stats["warned"],
            "rules_count": len(self.rules),
            "rules": {rid: dict(s) for rid, s in self.rule_stats.items()},
        }
