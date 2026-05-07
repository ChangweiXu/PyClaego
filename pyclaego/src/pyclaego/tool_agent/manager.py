"""ToolAgentManager — 子代理配置管理器（单例模式）

遵循 SKILL 的目录分层模式：
  builtin/   → 随项目发布的内置子代理（优先级最低）
  global/    → 用户全局自定义子代理
  PS/        → PersonalSpace 级子代理
  Widget/    → Widget 级子代理（优先级最高）

与 SkillManager 的关键区别：
  - 不维护 per-widget 内部状态，而是提供 resolve_for_widget() 返回 snapshot
  - Widget 持有解析后的 snapshot dict，生命周期由 Widget 管理
  - 修改 tool_agents 目录后，Widget 重新调用 resolve_for_widget() 即可刷新

单例模式：全局共享 builtin + global 层的解析缓存。
"""
from __future__ import annotations

from pathlib import Path

from ..config import PYCLAEGO_DEFAULT_ROOT, get_config
from ..logging import get_running_log
from .config import ToolAgentConfig

_rlog = get_running_log()


class ToolAgentManager:
    """子代理配置管理器（单例）。

    职责：
      - 从磁盘目录发现并解析 config.json 文件
      - 按优先级合并多层配置（builtin < global < PS < widget）
      - 提供 resolve_for_widget() 接口返回 Widget 可用的快照

    Attributes:
        tool_agent_directories: 全局 tool_agent 目录列表（builtin + user custom）
    """

    _instance: ToolAgentManager | None = None

    def __new__(cls) -> ToolAgentManager:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return

        config = get_config()
        ta_config = config.get("tool_agents", {}) or {}

        # 目录列表 — 默认只有 builtin
        directories = ta_config.get("directories", [])
        if directories:
            self.tool_agent_directories = [Path(d).expanduser() for d in directories]
        else:
            self.tool_agent_directories = [
                Path(PYCLAEGO_DEFAULT_ROOT) / "tool_agents" / "builtin",
            ]

        self.cache_enabled: bool = ta_config.get("cache_enabled", True)

        # ── 分层缓存：layer -> {name: ToolAgentConfig} ──
        # layer: "builtin" | "global" | ps:<ps_id> | widget:<ps_id>:<widget_id>
        self._layer_cache: dict[str, dict[str, ToolAgentConfig]] = {}

        self._initialized = True
        _rlog.info(
            "core_service",
            f"[ToolAgentManager] 单例已初始化，目录: "
            f"{[str(d) for d in self.tool_agent_directories]}",
        )

    # ------------------------------------------------------------------
    # 单例访问
    # ------------------------------------------------------------------

    @classmethod
    def get_instance(cls) -> ToolAgentManager:
        """获取 ToolAgentManager 单例实例。"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ------------------------------------------------------------------
    # 目录扫描
    # ------------------------------------------------------------------

    def load_builtins(self) -> int:
        """加载 builtin 层 tool_agents。

        Returns:
            成功加载的数量
        """
        agents: dict[str, ToolAgentConfig] = {}
        for directory in self.tool_agent_directories:
            scanned = self._scan_directory(directory)
            # 同层内后扫描的覆盖先扫描的
            agents.update(scanned)
        self._layer_cache["builtin"] = agents
        _rlog.info(
            "core_service",
            f"[ToolAgentManager] builtin 层加载完成: {len(agents)} 个",
        )
        return len(agents)

    def load_globals(self) -> int:
        """加载 global 自定义层 tool_agents。

        从 tool_agents.directories 中排除 builtin 目录后的目录加载。
        通常这些目录与 builtin 同级但优先级更高。

        Returns:
            成功加载的数量
        """
        # global 层：directories 中非 builtin 的目录
        # builtin 默认目录
        builtin_dir = Path(PYCLAEGO_DEFAULT_ROOT) / "tool_agents" / "builtin"
        global_dirs = [
            d for d in self.tool_agent_directories
            if d.resolve() != builtin_dir.resolve()
        ]
        if not global_dirs:
            # 默认也扫描 ~/pyclaego/tool_agents/（builtin 的父目录，但排除 builtin 子目录）
            parent_dir = Path(PYCLAEGO_DEFAULT_ROOT) / "tool_agents"
            if parent_dir.is_dir():
                global_dirs = [parent_dir]

        agents: dict[str, ToolAgentConfig] = {}
        for directory in global_dirs:
            scanned = self._scan_directory(directory)
            # 排除 builtin 已加载的（builtin 子目录不应被 global 重复计算）
            for name, cfg in scanned.items():
                if name not in agents:
                    agents[name] = cfg

        self._layer_cache["global"] = agents
        _rlog.info(
            "core_service",
            f"[ToolAgentManager] global 层加载完成: {len(agents)} 个",
        )
        return len(agents)

    def _scan_directory(self, directory: Path) -> dict[str, ToolAgentConfig]:
        """扫描单个目录，发现并解析所有 config.json。

        Args:
            directory: 要扫描的目录

        Returns:
            {name: ToolAgentConfig} 字典
        """
        directory = directory.expanduser().resolve()
        if not directory.is_dir():
            return {}

        agents: dict[str, ToolAgentConfig] = {}
        for entry in sorted(directory.iterdir()):
            if not entry.is_dir():
                continue
            config_file = entry / "config.json"
            if not config_file.exists():
                continue

            try:
                cfg = ToolAgentConfig.from_json(config_file)
                agents[cfg.name] = cfg
                _rlog.info(
                    "core_service",
                    f"[ToolAgentManager] 发现 tool_agent: {cfg.name} "
                    f"(subagent_type={cfg.subagent_type}, "
                    f"context_strategy={cfg.context_strategy})",
                )
            except Exception as exc:
                _rlog.error(
                    "core_service",
                    f"[ToolAgentManager] 加载 tool_agent 失败 "
                    f"{config_file}: {exc}",
                )
        return agents

    # ------------------------------------------------------------------
    # Widget 级解析（核心 API）
    # ------------------------------------------------------------------

    def resolve_for_widget(
        self,
        ps_id: str,
        widget_id: str,
    ) -> dict[str, ToolAgentConfig]:
        """为指定 Widget 解析完整的 tool_agent 列表。

        合并优先级（低→高）：
          1. builtin 层
          2. global 层
          3. PS 层：<ps_root>/<ps_id>/tool_agents/
          4. Widget 层：<ps_root>/<ps_id>/widgets/<widget_id>/tool_agents/

        Args:
            ps_id: PersonalSpace ID
            widget_id: Widget ID

        Returns:
            {name: ToolAgentConfig} 合并后的快照
        """
        # 确保 builtin 和 global 已加载
        if "builtin" not in self._layer_cache:
            self.load_builtins()
        if "global" not in self._layer_cache:
            self.load_globals()

        # 按优先级逐层合并
        merged: dict[str, ToolAgentConfig] = {}

        # Layer 1: builtin
        merged.update(self._layer_cache.get("builtin", {}))

        # Layer 2: global
        merged.update(self._layer_cache.get("global", {}))

        # Layer 3: PS
        ps_agents = self._load_ps_layer(ps_id)
        merged.update(ps_agents)

        # Layer 4: Widget（最高优先级）
        widget_agents = self._load_widget_layer(ps_id, widget_id)
        merged.update(widget_agents)

        return merged

    def _load_ps_layer(self, ps_id: str) -> dict[str, ToolAgentConfig]:
        """加载 PS 层 tool_agents。"""
        cache_key = f"ps:{ps_id}"
        if cache_key in self._layer_cache:
            return self._layer_cache[cache_key]

        config = get_config()
        ps_root_str = config.get("personal_space.root_path") or str(
            Path(PYCLAEGO_DEFAULT_ROOT) / "personal_spaces"
        )
        ps_dir = Path(ps_root_str).expanduser() / ps_id / "tool_agents"

        agents = self._scan_directory(ps_dir)
        self._layer_cache[cache_key] = agents
        if agents:
            _rlog.info(
                "core_service",
                f"[ToolAgentManager] PS 层 ({ps_id}) 加载完成: {len(agents)} 个",
            )
        return agents

    def _load_widget_layer(self, ps_id: str, widget_id: str) -> dict[str, ToolAgentConfig]:
        """加载 Widget 层 tool_agents。"""
        cache_key = f"widget:{ps_id}:{widget_id}"
        if cache_key in self._layer_cache:
            return self._layer_cache[cache_key]

        config = get_config()
        ps_root_str = config.get("personal_space.root_path") or str(
            Path(PYCLAEGO_DEFAULT_ROOT) / "personal_spaces"
        )
        widget_dir = (
            Path(ps_root_str).expanduser() / ps_id / "widgets" / widget_id / "tool_agents"
        )

        agents = self._scan_directory(widget_dir)
        self._layer_cache[cache_key] = agents
        if agents:
            _rlog.info(
                "core_service",
                f"[ToolAgentManager] Widget 层 ({ps_id}/{widget_id}) "
                f"加载完成: {len(agents)} 个",
            )
        return agents

    # ------------------------------------------------------------------
    # 查询 API
    # ------------------------------------------------------------------

    def get_agent(
        self,
        name: str,
        ps_id: str | None = None,
        widget_id: str | None = None,
    ) -> ToolAgentConfig | None:
        """按名称查找单个 ToolAgentConfig。

        Args:
            name: tool_agent 名称
            ps_id: PS ID（可选，提供则包括 PS 层）
            widget_id: Widget ID（可选，提供则包括 Widget 层）

        Returns:
            ToolAgentConfig 或 None
        """
        if ps_id and widget_id:
            resolved = self.resolve_for_widget(ps_id, widget_id)
        elif ps_id:
            resolved = self._resolve_for_ps(ps_id)
        else:
            resolved = self._resolve_global()
        return resolved.get(name)

    def list_agent_names(
        self,
        ps_id: str | None = None,
        widget_id: str | None = None,
    ) -> list[str]:
        """列出所有可用的 tool_agent 名称。

        Args:
            ps_id: PS ID（可选）
            widget_id: Widget ID（可选）

        Returns:
            排序后的名称列表
        """
        if ps_id and widget_id:
            resolved = self.resolve_for_widget(ps_id, widget_id)
        elif ps_id:
            resolved = self._resolve_for_ps(ps_id)
        else:
            resolved = self._resolve_global()
        return sorted(resolved.keys())

    def _resolve_global(self) -> dict[str, ToolAgentConfig]:
        """仅合并 builtin + global 层。"""
        if "builtin" not in self._layer_cache:
            self.load_builtins()
        if "global" not in self._layer_cache:
            self.load_globals()
        merged: dict[str, ToolAgentConfig] = {}
        merged.update(self._layer_cache.get("builtin", {}))
        merged.update(self._layer_cache.get("global", {}))
        return merged

    def _resolve_for_ps(self, ps_id: str) -> dict[str, ToolAgentConfig]:
        """合并 builtin + global + PS 层。"""
        merged = self._resolve_global()
        merged.update(self._load_ps_layer(ps_id))
        return merged

    # ------------------------------------------------------------------
    # SUBAGENT_PROFILES 注册
    # ------------------------------------------------------------------

    def register_all_to_subagent_profiles(self) -> int:
        """将所有已加载的 tool_agents 注册到 SUBAGENT_PROFILES。

        仅注册 builtin + global 层（PS/Widget 层由 Widget 负责按需注册）。
        直接注册 ToolAgentConfig（不再转换为 SubagentProfile）。

        Returns:
            成功注册的数量
        """
        from .registry import SUBAGENT_PROFILES, register_profile

        merged = self._resolve_global()
        count = 0
        for name, cfg in merged.items():
            try:
                if name in SUBAGENT_PROFILES:
                    _rlog.info(
                        "core_service",
                        f"[ToolAgentManager] tool_agent '{name}' 已在 "
                        f"SUBAGENT_PROFILES 中，跳过注册",
                    )
                    continue
                register_profile(cfg)
                count += 1
            except Exception as exc:
                _rlog.error(
                    "core_service",
                    f"[ToolAgentManager] 注册 tool_agent '{name}' 到 "
                    f"SUBAGENT_PROFILES 失败: {exc}",
                )
        _rlog.info(
            "core_service",
            f"[ToolAgentManager] 已注册 {count} 个 tool_agent 到 SUBAGENT_PROFILES",
        )
        return count

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    def resolve_skills_for_agent(
        self,
        cfg: ToolAgentConfig,
        session_id: str,
    ) -> list[str]:
        """解析 tool_agent 的技能列表。

        处理 "*" 通配符 → 返回所有可用技能名称。

        Args:
            cfg: ToolAgentConfig
            session_id: 当前 session ID

        Returns:
            解析后的技能名称列表
        """
        if cfg.uses_all_skills:
            try:
                from ..skill import get_skill_manager
                sm = get_skill_manager()
                return sm.list_skills(session_id=session_id)
            except Exception as exc:
                _rlog.warning(
                    "core_service",
                    f"[ToolAgentManager] 解析 skills='*' 失败: {exc}",
                )
                return []
        return list(cfg.skills)

    def clear_cache(self) -> None:
        """清空所有层缓存（包括 builtin/global/PS/Widget）。"""
        self._layer_cache.clear()
        _rlog.info("core_service", "[ToolAgentManager] 缓存已清空")

    def reload(self) -> tuple[int, int]:
        """重新加载 builtin + global 层。

        Returns:
            (builtin_count, global_count)
        """
        self._layer_cache.pop("builtin", None)
        self._layer_cache.pop("global", None)
        builtin_count = self.load_builtins()
        global_count = self.load_globals()
        return builtin_count, global_count

    def __repr__(self) -> str:
        builtin_count = len(self._layer_cache.get("builtin", {}))
        global_count = len(self._layer_cache.get("global", {}))
        return (
            f"<ToolAgentManager builtin={builtin_count} global={global_count}>"
        )
