"""配置管理器模块

支持从多个位置读取配置文件：
1. ~/.pyclaego/config.yaml (用户主目录)
2. ./config.yaml (当前目录)

支持环境变量替换：
- 使用 ${ENV_VAR} 语法引用环境变量
- 支持默认值: ${ENV_VAR:default_value}

支持配置项引用和拼接：
- 使用 @{config.key} 引用其他配置项
- 使用 !concat ["literal", "@{key1}", "@{key2}"] 拼接多个值

支持路径转换：
- 使用 !abs_path "path" 转换为绝对路径（支持 ~/ 展开）

支持配置文件拆分：
- 使用 !include "sub_config_path" 引入单个子配置文件（替换当前节点）
- 使用 !include_dir "dir_path" 合并目录内所有 .yaml/.yml 文件到当前节点
- 路径支持 ~/、./（相对于当前配置文件）和绝对路径
- !include_dir 按文件名字典序加载，后者覆盖前者（last-wins）
- 循环引入会抛出 ValueError；文件/目录不存在会抛出 FileNotFoundError
"""

import os
import re
from pathlib import Path
from typing import Any

import yaml
from yaml import SafeLoader

_PYCLAEGO_HOME = Path.home() / ".pyclaego"

# 哨兵对象，用于区分 "键不存在" 和 "值为 None"
_MISSING = object()


class ConcatTag:
    """!concat 标签数据类"""
    def __init__(self, values: list[Any]):
        self.values = values


class AbsPathTag:
    """!abs_path 标签数据类"""
    def __init__(self, path: str):
        self.path = path


class JoinPathTag:
    """!join_path 标签数据类"""
    def __init__(self, parts: list[Any]):
        self.parts = parts


class IncludeTag:
    """!include 标签数据类 — 替换当前节点为指定文件的内容"""
    def __init__(self, path: str):
        self.path = path


class IncludeDirTag:
    """!include_dir 标签数据类 — 合并目录内所有 .yaml/.yml 文件到当前节点"""
    def __init__(self, path: str):
        self.path = path


class IncludeMergeTag:
    """!include_merge 标签数据类 — 引入文件并将其键值对合并到父 dict 层级

    用法：在 dict 中以任意唯一键名作为哨兵 key：

        parent:
          existing_key: value
          _ext1: !include_merge "extra.yaml"   # key 名任意，结果中不会出现
          _ext2: !include_merge "more.yaml"

    被引入文件的顶层必须是 dict，否则抛出 ConfigIncludeError。
    合并顺序遵循 YAML 中出现的顺序（Python 3.7+ 字典插入顺序）。
    """
    def __init__(self, path: str):
        self.path = path


class ConfigIncludeError(Exception):
    """!include / !include_dir 标签解析错误

    用于区分"引入文件不存在/循环引用/格式错误"等明确的用户配置错误
    与其他运行时异常，使 _load_config 可以选择性地向上抛出而不是静默回退。
    """
    pass


def concat_constructor(loader: SafeLoader, node: yaml.Node) -> ConcatTag:
    """YAML !concat 标签构造器"""
    if not isinstance(node, yaml.SequenceNode):
        raise ValueError(f"!concat 标签必须是一个序列，但收到 {type(node)}")
    values = loader.construct_sequence(node)
    return ConcatTag(values)


def abs_path_constructor(loader: SafeLoader, node: yaml.Node) -> AbsPathTag:
    """YAML !abs_path 标签构造器"""
    if not isinstance(node, yaml.ScalarNode):
        raise ValueError(f"!abs_path 标签必须是一个标量，但收到 {type(node)}")
    path = loader.construct_scalar(node)
    return AbsPathTag(path)


def join_path_constructor(loader: SafeLoader, node: yaml.Node) -> JoinPathTag:
    """YAML !join_path 标签构造器"""
    if not isinstance(node, yaml.SequenceNode):
        raise ValueError(f"!join_path 标签必须是一个序列，但收到 {type(node)}")
    parts = loader.construct_sequence(node)
    return JoinPathTag(parts)


def include_constructor(loader: SafeLoader, node: yaml.Node) -> IncludeTag:
    """YAML !include 标签构造器"""
    if not isinstance(node, yaml.ScalarNode):
        raise ValueError(f"!include 标签必须是一个标量（路径字符串），但收到 {type(node)}")
    path = loader.construct_scalar(node)
    return IncludeTag(path)


def include_dir_constructor(loader: SafeLoader, node: yaml.Node) -> IncludeDirTag:
    """YAML !include_dir 标签构造器"""
    if not isinstance(node, yaml.ScalarNode):
        raise ValueError(f"!include_dir 标签必须是一个标量（目录路径字符串），但收到 {type(node)}")
    path = loader.construct_scalar(node)
    return IncludeDirTag(path)


def include_merge_constructor(loader: SafeLoader, node: yaml.Node) -> IncludeMergeTag:
    """YAML !include_merge 标签构造器"""
    if not isinstance(node, yaml.ScalarNode):
        raise ValueError(f"!include_merge 标签必须是一个标量（路径字符串），但收到 {type(node)}")
    path = loader.construct_scalar(node)
    return IncludeMergeTag(path)


# 注册自定义 YAML 标签
yaml.add_constructor('!concat', concat_constructor, SafeLoader)
yaml.add_constructor('!abs_path', abs_path_constructor, SafeLoader)
yaml.add_constructor('!join_path', join_path_constructor, SafeLoader)
yaml.add_constructor('!include', include_constructor, SafeLoader)
yaml.add_constructor('!include_dir', include_dir_constructor, SafeLoader)
yaml.add_constructor('!include_merge', include_merge_constructor, SafeLoader)


class ConfigManager:
    """配置管理器 - 支持环境变量替换和配置项引用
    
    功能：
    - 按优先级顺序读取配置文件
    - 提供便捷的配置项访问方法
    - 支持默认值
    - 支持环境变量替换 ${ENV_VAR} 或 ${ENV_VAR:default}
    - 支持配置项引用 *config.key
    - 支持拼接 !concat [*key1, "literal", *key2]
    """
    
    # 环境变量匹配正则: ${VAR} 或 ${VAR:default}
    ENV_VAR_PATTERN = re.compile(r'\$\{([^}:]+)(?::([^}]*))?\}')
    
    # 配置项引用正则: @{config.key.path}
    CONFIG_REF_PATTERN = re.compile(r'@\{([a-zA-Z0-9_.]+)\}')
    
    # 配置文件搜索路径（按优先级顺序）
    CONFIG_PATHS = [
        Path(_PYCLAEGO_HOME) / "config.yaml",       # ~/pyclaego/config.yaml
        Path(__file__).parent.parent.parent / Path("config.yaml"),    # ./config.yaml
    ]
    
    # 默认配置
    DEFAULT_CONFIG = {
        "server": {
            "host": "127.0.0.1",
            "port": 8765,
            "max_connections": 100
        },
        "client": {
            "reconnect_interval": 3,
            "timeout": 10
        },
        "logging": {
            "level": "INFO",
            "format": "text"
        }
    }
    
    def __init__(self, config_path: str | None = None):
        """初始化配置管理器
        
        Args:
            config_path: 可选的配置文件路径，如果提供则优先使用
        """
        self.config: dict[str, Any] = self._deep_copy(self.DEFAULT_CONFIG)
        self.config_file: Path | None = None
        self._resolving_keys: set = set()  # 循环引用检测
        
        # 如果提供了指定路径，优先使用
        if config_path:
            custom_path = Path(config_path)
            if custom_path.exists():
                self.config_file = custom_path
                self._load_config(custom_path)
                return
        
        # 按顺序搜索配置文件
        for path in self.CONFIG_PATHS:
            if path.exists():
                self.config_file = path
                self._load_config(path)
                break
        
        # 如果没有找到配置文件，使用默认配置
        if not self.config_file:
            print("[Config] 未找到配置文件，使用默认配置")
            print("[Config] 可在以下位置创建配置文件:")
            for path in self.CONFIG_PATHS:
                print(f"  - {path}")
    
    def _load_config(self, path: Path) -> None:
        """从文件加载配置
        
        Args:
            path: 配置文件路径
        """
        try:
            with open(path, encoding='utf-8') as f:
                loaded_config = yaml.safe_load(f)
                
            if loaded_config:
                # 0. 解析 !include / !include_dir 标签（必须在其他处理之前）
                loaded_config = self._resolve_includes(
                    loaded_config,
                    base_dir=path.parent.resolve(),
                    include_stack={str(path.resolve())},
                )
                
                # 1. 环境变量替换
                loaded_config = self._replace_env_vars(loaded_config)
                
                # 2. 深度合并配置（用户配置覆盖默认配置）
                self._deep_merge(self.config, loaded_config)
                
                # 3. 配置项引用和拼接解析（在合并后执行，确保所有配置项都可用）
                self._resolve_config_references(self.config)
                
            print(f"[Config] 已加载配置文件: {path}")
            
        except ConfigIncludeError:
            # !include / !include_dir 错误属于明确的配置错误，不静默回退
            raise
        except Exception as e:
            print(f"[Config] 加载配置文件失败: {e}")
            print("[Config] 使用默认配置")
            import traceback
            traceback.print_exc()

    def _resolve_includes(
        self,
        obj: Any,
        base_dir: Path,
        include_stack: set,
    ) -> Any:
        """递归解析 !include / !include_dir 标签，将其替换为实际的 YAML 内容。

        此方法必须在 _replace_env_vars 和 _resolve_config_references 之前调用，
        因为被引入的文件可能包含 ${VAR} 和 @{ref} 表达式，需要在统一树上进行后续处理。

        路径解析规则：
        - 相对路径（./foo、bar）相对于当前包含文件所在目录
        - ~/ 路径展开为用户主目录
        - 绝对路径直接使用

        Args:
            obj: 当前配置节点（可能包含 IncludeTag / IncludeDirTag）
            base_dir: 当前文件所在目录（用于解析相对路径）
            include_stack: 已处理过的绝对路径集合（用于循环引用检测）

        Returns:
            替换 IncludeTag/IncludeDirTag 后的节点

        Raises:
            ValueError: 检测到循环引用，或 !include_dir 中的文件内容不是字典
            FileNotFoundError: 指定的文件或目录不存在
        """
        if isinstance(obj, IncludeTag):
            return self._load_include_file(obj.path, base_dir, include_stack)

        elif isinstance(obj, IncludeDirTag):
            return self._load_include_dir(obj.path, base_dir, include_stack)

        elif isinstance(obj, dict):
            # Two-pass: first build result for normal keys, then apply !include_merge
            result: dict = {}
            merge_queue: list = []  # list of IncludeMergeTag values in YAML order
            for key, value in obj.items():
                if isinstance(value, IncludeMergeTag):
                    # Collect merge tags; sentinel key is intentionally dropped
                    merge_queue.append(value)
                else:
                    result[key] = self._resolve_includes(value, base_dir, include_stack)
            for tag in merge_queue:
                content = self._load_include_file(tag.path, base_dir, include_stack)
                if not isinstance(content, dict):
                    raise ConfigIncludeError(
                        f"!include_merge 要求引入文件的顶层必须是 dict，"
                        f"但 '{tag.path}' 的内容是 {type(content).__name__}"
                    )
                self._deep_merge(result, content)
            return result

        elif isinstance(obj, list):
            return [self._resolve_includes(item, base_dir, include_stack) for item in obj]

        else:
            return obj

    def _resolve_include_path(self, raw_path: str, base_dir: Path) -> Path:
        """解析 !include / !include_dir 中的路径为绝对 Path。

        Args:
            raw_path: 原始路径字符串（支持 ~/、./、绝对路径）
            base_dir: 当前文件所在目录

        Returns:
            解析后的绝对 Path
        """
        expanded = os.path.expanduser(raw_path)
        p = Path(expanded)
        if not p.is_absolute():
            p = base_dir / p
        return p.resolve()

    def _load_include_file(self, raw_path: str, base_dir: Path, include_stack: set) -> Any:
        """加载单个 !include 文件。

        Args:
            raw_path: !include 中的路径字符串
            base_dir: 当前文件所在目录
            include_stack: 已处理过的绝对路径集合

        Returns:
            被引入文件解析后的内容（任意类型）

        Raises:
            FileNotFoundError: 文件不存在
            ValueError: 检测到循环引用
        """
        target = self._resolve_include_path(raw_path, base_dir)
        target_str = str(target)

        if not target.exists():
            raise ConfigIncludeError(
                f"!include 目标文件不存在: {target}"
            )

        if target_str in include_stack:
            chain = " -> ".join(include_stack) + f" -> {target_str}"
            raise ConfigIncludeError(f"检测到循环 !include 引用: {chain}")

        with open(target, encoding='utf-8') as f:
            content = yaml.safe_load(f)

        if content is None:
            return None

        # 递归解析子文件中的 !include，以子文件目录为 base_dir
        new_stack = include_stack | {target_str}
        return self._resolve_includes(content, base_dir=target.parent, include_stack=new_stack)

    def _load_include_dir(self, raw_path: str, base_dir: Path, include_stack: set) -> dict[str, Any]:
        """加载 !include_dir 目录内所有 .yaml/.yml 文件并合并。

        加载顺序：按文件名字典序排序，后加载的文件覆盖先加载的同名键（last-wins）。
        目录内每个文件的顶层必须是映射（dict），否则抛出 ValueError。

        Args:
            raw_path: !include_dir 中的目录路径字符串
            base_dir: 当前文件所在目录
            include_stack: 已处理过的绝对路径集合

        Returns:
            合并后的字典

        Raises:
            FileNotFoundError: 目录不存在
            ValueError: 某个文件内容不是字典
        """
        target_dir = self._resolve_include_path(raw_path, base_dir)

        if not target_dir.exists():
            raise ConfigIncludeError(
                f"!include_dir 目标目录不存在: {target_dir}"
            )
        if not target_dir.is_dir():
            raise ConfigIncludeError(
                f"!include_dir 路径不是目录: {target_dir}"
            )

        # 收集所有 yaml/yml 文件，按文件名排序
        yaml_files = sorted(
            [p for p in target_dir.iterdir() if p.suffix in ('.yaml', '.yml') and p.is_file()],
            key=lambda p: p.name,
        )

        merged: dict[str, Any] = {}
        for yaml_file in yaml_files:
            target_str = str(yaml_file)
            if target_str in include_stack:
                chain = " -> ".join(include_stack) + f" -> {target_str}"
                raise ConfigIncludeError(f"检测到循环 !include_dir 引用: {chain}")

            with open(yaml_file, encoding='utf-8') as f:
                content = yaml.safe_load(f)

            if content is None:
                continue

            if not isinstance(content, dict):
                raise ConfigIncludeError(
                    f"!include_dir 要求每个文件顶层必须是字典，"
                    f"但 {yaml_file.name} 的内容是 {type(content).__name__}"
                )

            # 递归解析子文件中的 !include
            new_stack = include_stack | {target_str}
            content = self._resolve_includes(content, base_dir=yaml_file.parent, include_stack=new_stack)

            self._deep_merge(merged, content)

        return merged
    
    def _replace_env_vars(self, obj: Any) -> Any:
        """递归替换配置中的环境变量
        
        支持格式：
        - ${ENV_VAR} - 使用环境变量，如果不存在则报错
        - ${ENV_VAR:default} - 使用环境变量，如果不存在则使用默认值
        
        Args:
            obj: 配置对象（可以是字典、列表、字符串等）
            
        Returns:
            替换后的对象
        """
        if isinstance(obj, dict):
            return {key: self._replace_env_vars(value) for key, value in obj.items()}
        
        elif isinstance(obj, list):
            return [self._replace_env_vars(item) for item in obj]
        
        elif isinstance(obj, str):
            return self._replace_env_var_in_string(obj)
        
        elif isinstance(obj, ConcatTag):
            # 保留 ConcatTag，稍后处理
            obj.values = [self._replace_env_vars(v) for v in obj.values]
            return obj
        
        elif isinstance(obj, AbsPathTag):
            # 保留 AbsPathTag，稍后处理
            # 路径必须是字符串，防止类型自动转换产生非字符串值
            path_val = self._replace_env_var_in_string(obj.path)
            obj.path = str(path_val) if not isinstance(path_val, str) else path_val
            return obj
        
        elif isinstance(obj, JoinPathTag):
            # 保留 JoinPathTag，稍后处理
            obj.parts = [self._replace_env_vars(p) for p in obj.parts]
            return obj

        elif isinstance(obj, (IncludeTag, IncludeDirTag, IncludeMergeTag)):
            # !include / !include_dir / !include_merge 应在此步骤之前已被 _resolve_includes 消除；
            # 若仍存在则说明是代码 bug，直接透传以便后续报错定位。
            return obj
        
        else:
            return obj
    
    def _replace_env_var_in_string(self, text: str) -> str | int | float | bool:
        """替换字符串中的环境变量
        
        Args:
            text: 包含环境变量的字符串
            
        Returns:
            替换后的值（自动转换类型）
        """
        if not isinstance(text, str):
            raise ValueError(f"环境变量替换仅支持字符串类型，但收到 {type(text)}")

        def replacer(match):
            var_name = match.group(1)
            default_value = match.group(2)
            
            # 获取环境变量
            value = os.environ.get(var_name)
            
            if value is None:
                if default_value is not None:
                    # 使用默认值
                    return default_value
                else:
                    # 没有默认值，报错
                    raise ValueError(
                        f"环境变量 '{var_name}' 未设置，且没有提供默认值。"
                        f"请设置环境变量或在配置中使用 ${{{var_name}:default_value}} 格式。"
                    )
            
            return value
        
        # 替换所有环境变量
        result = self.ENV_VAR_PATTERN.sub(replacer, text)
        
        # 仅当整个字符串是单个环境变量引用时，才尝试类型转换
        # 避免 "${A:http}://${B:host}" 这样的多变量字符串被误转换
        if self.ENV_VAR_PATTERN.fullmatch(text):
            return self._try_convert_type(result)
        
        return result
    
    def _resolve_config_references(self, obj: Any, visited: set | None = None) -> Any:
        """递归解析配置项引用和拼接
        
        Args:
            obj: 配置对象
            visited: 已访问的对象（防止循环引用）
            
        Returns:
            解析后的对象
        """
        if visited is None:
            visited = set()
        
        # 防止循环引用
        obj_id = id(obj)
        if obj_id in visited:
            return obj
        visited.add(obj_id)
        
        if isinstance(obj, dict):
            for key, value in obj.items():
                obj[key] = self._resolve_config_references(value, visited)
            return obj
        
        elif isinstance(obj, list):
            return [self._resolve_config_references(item, visited) for item in obj]
        
        elif isinstance(obj, ConcatTag):
            # 处理 !concat 标签
            return self._process_concat(obj.values)
        
        elif isinstance(obj, AbsPathTag):
            # 处理 !abs_path 标签
            return self._process_abs_path(obj.path)
        
        elif isinstance(obj, JoinPathTag):
            # 处理 !join_path 标签
            return self._process_join_path(obj.parts)

        elif isinstance(obj, (IncludeTag, IncludeDirTag, IncludeMergeTag)):
            # 应在 _resolve_includes 阶段已消除；若仍存在则是 bug
            raise RuntimeError(
                f"未解析的 {type(obj).__name__} 节点（路径: {obj.path!r}）在配置引用解析阶段仍然存在，"
                "请检查 _load_config 是否正确调用了 _resolve_includes。"
            )

        elif isinstance(obj, str):
            # 处理字符串中的配置项引用
            return self._resolve_config_ref_in_string(obj)
        
        else:
            return obj
    
    def _process_concat(self, values: list[Any]) -> str:
        """处理 !concat 拼接
        
        Args:
            values: 要拼接的值列表
            
        Returns:
            拼接后的字符串
            
        Examples:
            >>> _process_concat(["ws://", "@{server.host}", ":", "@{server.port}"])
            "ws://127.0.0.1:8765"
        """
        result_parts = []
        
        for value in values:
            if isinstance(value, (ConcatTag, AbsPathTag, JoinPathTag)):
                # 解析嵌套的 Tag 对象
                resolved = self._resolve_config_references(value)
                result_parts.append(str(resolved))
            elif isinstance(value, str):
                # 解析字符串中的配置项引用
                resolved = self._resolve_config_ref_in_string(value)
                result_parts.append(str(resolved))
            else:
                result_parts.append(str(value))
        
        return ''.join(result_parts)
    
    def _process_abs_path(self, path: str) -> str:
        """处理 !abs_path 路径转换
        
        Args:
            path: 路径字符串（可能包含 ~/ 或相对路径）
            
        Returns:
            绝对路径字符串
            
        Examples:
            >>> _process_abs_path("~/pyclaego/workspaces")
            "/Users/username/pyclaego/workspaces"
            >>> _process_abs_path("./workspaces")
            "/current/dir/workspaces"
        """
        # 先解析配置项引用（如果有）
        if '@{' in path:
            resolved = self._resolve_config_ref_in_string(path)
            if not isinstance(resolved, str):
                raise ValueError(f"!abs_path 标签的路径必须解析为字符串，但得到 {type(resolved)}")
            path = resolved
        
        # 展开 ~ 为用户主目录
        path = os.path.expanduser(str(path))
        
        # 转换为绝对路径
        abs_path = os.path.abspath(path)
        
        return abs_path
    
    def _process_join_path(self, parts: list[Any]) -> str:
        """处理 !join_path 路径拼接并解析为绝对路径
        
        Args:
            parts: 路径组件列表
            
        Returns:
            解析后的绝对路径字符串
            
        Examples:
            >>> _process_join_path(["@{pyclaego.root_path}", "logs", "app.log"])
            "/home/user/pyclaego/logs/app.log"
        """
        resolved_parts = []
        for part in parts:
            # Resolve nested tags (e.g. AbsPathTag, ConcatTag) before stringifying
            if isinstance(part, (ConcatTag, AbsPathTag, JoinPathTag)):
                part = self._resolve_config_references(part)
            if isinstance(part, str):
                part = self._resolve_config_ref_in_string(part)
            resolved_parts.append(str(part))
        
        joined = os.path.join(*resolved_parts) if resolved_parts else ""
        joined = os.path.expanduser(joined)
        return os.path.abspath(joined)
    
    def _resolve_config_ref_in_string(self, text: str) -> str | int | float | bool:
        """解析字符串中的配置项引用
        
        Args:
            text: 包含配置项引用的字符串 (例如: "@{server.host}")
            
        Returns:
            替换后的值
        """
        def replacer(match):
            config_path = match.group(1)
            return str(self._resolve_single_ref(config_path))
        
        # 替换所有配置项引用
        result = self.CONFIG_REF_PATTERN.sub(replacer, text)
        
        # 如果整个字符串都是配置项引用，返回原始类型
        if text.startswith('@{') and text.endswith('}'):
            config_path = text[2:-1]  # 去掉 @{ 和 }
            return self._resolve_single_ref(config_path)
        
        return result
    
    def _resolve_single_ref(self, config_path: str) -> Any:
        """解析单个配置项引用，带循环检测和写回
        
        Args:
            config_path: 配置键路径 (例如: "server.host")
            
        Returns:
            解析后的值
        """
        # 循环引用检测
        if config_path in self._resolving_keys:
            raise ValueError(
                f"检测到循环配置引用: @{{{config_path}}}，"
                f"引用链: {' -> '.join(self._resolving_keys)} -> {config_path}"
            )
        
        # 使用哨兵区分 "键不存在" 和 "值为 None"
        value = self.get(config_path, default=_MISSING)
        if value is _MISSING:
            raise ValueError(f"配置项引用 '@{{{config_path}}}' 未找到")
        
        # None 值直接返回（允许引用 null 配置项）
        if value is None:
            return value
        
        # 如果引用的值仍是未解析的 Tag 对象，递归解析并写回
        if isinstance(value, (ConcatTag, AbsPathTag, JoinPathTag)):
            self._resolving_keys.add(config_path)
            try:
                value = self._resolve_config_references(value)
                # 写回 self.config，避免重复解析
                self._set(config_path, value)
            finally:
                self._resolving_keys.discard(config_path)
        
        # 如果引用的值是字符串且包含 @{} 引用，递归解析
        if isinstance(value, str) and '@{' in value:
            self._resolving_keys.add(config_path)
            try:
                value = self._resolve_config_ref_in_string(value)
                self._set(config_path, value)
            finally:
                self._resolving_keys.discard(config_path)
        
        return value
    
    def _set(self, key: str, value: Any) -> None:
        """设置配置项（支持点号路径），与 get 互为逆操作
        
        Args:
            key: 配置键路径 (例如: "server.host")
            value: 要设置的值
        """
        keys = key.split('.')
        target = self.config
        for k in keys[:-1]:
            if isinstance(target, dict) and k in target:
                target = target[k]
            else:
                return  # 路径不存在，跳过写回
        if isinstance(target, dict):
            target[keys[-1]] = value
    
    def _try_convert_type(self, value: str) -> str | int | float | bool:
        """尝试将字符串转换为合适的类型
        
        转换优先级: int > float > bool > str
        布尔值仅识别文字型: true/false/yes/no/on/off
        数字型 "0"/"1" 转为 int，不转为 bool
        
        Args:
            value: 字符串值
            
        Returns:
            转换后的值
        """
        # 整数（优先于布尔，避免 "0" → False, "1" → True）
        try:
            if '.' not in value:
                return int(value)
        except ValueError:
            pass
        
        # 浮点数
        try:
            return float(value)
        except ValueError:
            pass
        
        # 布尔值（仅文字型，不含 "0"/"1"）
        if value.lower() in ('true', 'yes', 'on'):
            return True
        if value.lower() in ('false', 'no', 'off'):
            return False
        
        # 保持字符串
        return value
    
    def _deep_copy(self, obj: Any) -> Any:
        """深度复制对象（包括自定义 Tag 对象）
        
        Args:
            obj: 要复制的对象
            
        Returns:
            复制后的对象
        """
        if isinstance(obj, dict):
            return {key: self._deep_copy(value) for key, value in obj.items()}
        elif isinstance(obj, list):
            return [self._deep_copy(item) for item in obj]
        elif isinstance(obj, ConcatTag):
            return ConcatTag([self._deep_copy(v) for v in obj.values])
        elif isinstance(obj, AbsPathTag):
            return AbsPathTag(obj.path)
        elif isinstance(obj, JoinPathTag):
            return JoinPathTag([self._deep_copy(p) for p in obj.parts])
        elif isinstance(obj, IncludeTag):
            return IncludeTag(obj.path)
        elif isinstance(obj, IncludeDirTag):
            return IncludeDirTag(obj.path)
        elif isinstance(obj, IncludeMergeTag):
            return IncludeMergeTag(obj.path)
        else:
            return obj
    
    def _deep_merge(self, base: dict, update: dict) -> None:
        """深度合并字典
        
        Args:
            base: 基础字典（会被修改）
            update: 更新字典
        """
        for key, value in update.items():
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                self._deep_merge(base[key], value)
            else:
                base[key] = value
    
    def get(self, key: str, default: Any = None) -> Any:
        """获取配置项（支持点号路径）
        
        Args:
            key: 配置键，支持点号分隔的路径（例如: "server.host"）
            default: 默认值
            
        Returns:
            配置值
            
        Examples:
            >>> config.get("server.host")
            "127.0.0.1"
            >>> config.get("server.port")
            8765
        """
        keys = key.split('.')
        value = self.config
        
        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default
        
        return value
    
    def get_server_config(self) -> dict[str, Any]:
        """获取服务器配置
        
        Returns:
            服务器配置字典
        """
        return self.config.get("server", {})
    
    def get_client_config(self) -> dict[str, Any]:
        """获取客户端配置
        
        Returns:
            客户端配置字典
        """
        return self.config.get("client", {})
    
    def get_logging_config(self) -> dict[str, Any]:
        """获取日志配置
        
        Returns:
            日志配置字典
        """
        return self.config.get("logging", {})
    
    def to_dict(self) -> dict[str, Any]:
        """返回完整配置字典的副本
        
        Returns:
            完整配置字典
        """
        return self._deep_copy(self.config)
    
    def show_config(self, mask_sensitive: bool = True) -> None:
        """显示当前配置（用于调试）
        
        Args:
            mask_sensitive: 是否屏蔽敏感信息（api_key, password 等）
        """
        print("\n" + "="*60)
        print("  当前配置")
        print("="*60)
        if self.config_file:
            print(f"配置文件: {self.config_file}")
        else:
            print("配置文件: [使用默认配置]")
        print("\n配置内容:")
        
        # 如果需要屏蔽敏感信息，创建副本
        if mask_sensitive:
            config_copy = self._deep_copy(self.config)
            self._mask_sensitive_data(config_copy)
            print(yaml.dump(config_copy, allow_unicode=True, default_flow_style=False))
        else:
            print(yaml.dump(self.config, allow_unicode=True, default_flow_style=False))
        
        print("="*60 + "\n")
    
    def _mask_sensitive_data(self, obj: Any) -> None:
        """屏蔽敏感数据（就地修改）
        
        Args:
            obj: 配置对象
        """
        if isinstance(obj, dict):
            for key, value in obj.items():
                # 检查键名是否包含敏感词
                if any(sensitive in key.lower() for sensitive in ['key', 'password', 'secret', 'token']):
                    if isinstance(value, str) and value:
                        obj[key] = "***MASKED***"
                else:
                    self._mask_sensitive_data(value)
        
        elif isinstance(obj, list):
            for item in obj:
                self._mask_sensitive_data(item)


# 全局配置实例（单例模式）
_global_config: ConfigManager | None = None


def get_config(config_path: str | None = None) -> ConfigManager:
    """获取全局配置实例
    
    Args:
        config_path: 可选的配置文件路径
        
    Returns:
        ConfigManager 实例
    """
    global _global_config
    
    if _global_config is None:
        _global_config = ConfigManager(config_path)
    
    return _global_config


def get_session_config(
    session_id: str,
    workspace_path: Path | None = None,
    config_path: str | None = None
) -> ConfigManager:
    """获取 Session 级配置实例
    
    执行流程:
    1. 获取全局配置 (通过 get_config)
    2. 在 Session 工作目录下查找 config.yaml
    3. 如果存在,读取并覆盖 agent 和 context 配置组
    4. 返回合并后的配置实例
    
    Args:
        session_id: Session ID
        workspace_path: Session 工作目录路径 (如果提供,优先使用)
        config_path: 可选的全局配置文件路径
        
    Returns:
        ConfigManager 实例(已合并 Session 级配置)
        
    Examples:
        >>> # 自动推断 Session 工作目录
        >>> config = get_session_config("abc123")
        
        >>> # 显式指定 Session 工作目录
        >>> config = get_session_config("abc123", workspace_path=Path("/custom/abc123"))
    """
    # 【2026年03月31日16:33:24新增】
    # 1. 获取全局配置
    global_config = get_config(config_path)
    
    # 2. 确定 Session 工作目录
    if workspace_path is None:
        # 如果未提供,根据配置推断
        session_config = global_config.get("session", {})
        session_workspace_root_dict = session_config.get("session_workspace_root", {})
        
        # 检查是否有自定义路径
        if session_workspace_root_dict and session_id in session_workspace_root_dict:
            workspace_path = Path(session_workspace_root_dict[session_id])
        else:
            # 使用默认路径
            workspace_root = session_config.get("workspace_root", "./workspaces")
            workspace_path = Path(workspace_root) / session_id
    
    # 3. 查找 Session 配置文件
    session_config_path = workspace_path / "config.yaml"
    
    # 如果文件不存在,返回全局配置
    if not session_config_path.exists():
        print(f"[Config] Session 配置文件不存在,使用全局配置 (Session: {session_id})")
        print(f"[Config] 查找路径: {session_config_path}")
        return global_config
    
    # 4. 读取 Session 配置文件
    try:
        with open(session_config_path, encoding='utf-8') as f:
            session_config_data = yaml.safe_load(f)
        
        if not session_config_data:
            print(f"[Config] Session 配置文件为空,使用全局配置 (Session: {session_id})")
            return global_config
        
        print(f"[Config] 已加载 Session 配置文件: {session_config_path}")
        
    except Exception as e:
        print(f"[Config] 读取 Session 配置文件失败: {e} (Session: {session_id})")
        print("[Config] 使用全局配置")
        import traceback
        traceback.print_exc()
        return global_config
    
    # 5. 处理 Session 配置中的环境变量和引用
    session_config_data = global_config._resolve_includes(
        session_config_data,
        base_dir=session_config_path.parent.resolve(),
        include_stack={str(session_config_path.resolve())},
    )
    session_config_data = global_config._replace_env_vars(session_config_data)
    
    # 6. 创建新的 ConfigManager 实例用于返回
    merged_config = ConfigManager.__new__(ConfigManager)
    merged_config.config = global_config._deep_copy(global_config.config)
    merged_config.config_file = global_config.config_file
    merged_config._resolving_keys = set()   # ← add this line
    
    # 7. 仅覆盖 agent、context、context_subagents、session_metadata 配置组
    for _merge_key in (
        "agent",
        "context",
        "context_subagents",
        "session_metadata",
        "cron",
    ):
        if _merge_key in session_config_data:
            print(f"[Config] 覆盖 {_merge_key} 配置 (Session: {session_id})")
            merged_config.config[_merge_key] = session_config_data[_merge_key]
    
    # 8. 解析配置项引用(如 @{llm.default_provider})
    merged_config._resolve_config_references(merged_config.config)
    
    print(f"[Config] Session 配置合并完成 (Session: {session_id})")
    return merged_config


# 测试代码
if __name__ == "__main__":
    # 测试配置项引用和拼接
    print("="*60)
    print("  测试配置项引用和拼接")
    print("="*60)
    
    # 测试配置管理器
    config = ConfigManager()
    config.show_config()
    
    # 测试获取配置
    print("测试配置访问:")
    print(f"server.host = {config.get('server.host')}")
    print(f"server.port = {config.get('server.port')}")
    print(f"client.server_url = {config.get('client.server_url')}")
    print(f"client.timeout = {config.get('client.timeout')}")
