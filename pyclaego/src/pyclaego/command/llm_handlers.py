"""LLM slash-command handler — /llm."""

from __future__ import annotations


async def handle_llm(args: list[str]) -> str:
    """/llm — 显示默认 LLM ID 及所有已配置的 provider 信息（name、api、model）。"""
    from ..config import get_config

    llm_cfg = get_config().get("llm") or {}
    default_provider: str = llm_cfg.get("default_provider") or "（未设置）"
    providers: dict = llm_cfg.get("providers") or {}

    if not providers:
        return (
            f"默认 LLM: {default_provider}\n"
            "（未找到任何 provider 配置）"
        )

    lines: list[str] = [
        f"默认 LLM: {default_provider}",
        "",
        f"{'ID':<30}  {'API':<25}  {'MODEL'}",
        f"{'-'*30}  {'-'*25}  {'-'*30}",
    ]
    for pid, pcfg in providers.items():
        if not isinstance(pcfg, dict):
            continue
        api = pcfg.get("api") or "—"
        model = pcfg.get("model") or "—"
        marker = " ◀ default" if pid == default_provider else ""
        lines.append(f"{pid:<30}  {api:<25}  {model}{marker}")

    return "\n".join(lines)


__all__ = ["handle_llm"]
