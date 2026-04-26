"""HistoryFileManager - 历史消息文件管理

统一封装 history.json / history.jsonl 两种格式的读写操作，
消除各 context handler 中的重复 I/O 代码。

支持格式：
  - history.json  : 单文件 JSON 数组（默认），整体读写，适合小型会话
  - history.jsonl : 每行一条 JSON 对象（JSON Lines），追加友好，适合大型会话

格式自动检测规则（以 workspace_path 为根目录）：
  1. 若 history.jsonl 文件存在，优先使用 jsonl 格式
  2. 若 history.json 文件存在，使用 json 格式
  3. 若两者均不存在，使用 json 格式（新建时默认）

可通过构造函数 format 参数强制指定格式：
  - "json"  : 强制使用 history.json
  - "jsonl" : 强制使用 history.jsonl
  - "auto"  : 自动检测（默认）
"""

import json
from pathlib import Path
from typing import Any, Dict, Iterator, List, Literal, Optional

from ..logging import get_running_log

_rlog = get_running_log()

HistoryFormat = Literal["json", "jsonl", "auto"]


class HistoryFileManager:
    """历史消息文件管理器

    职责：
    - 读取全量 / 部分历史消息
    - 写回（覆盖）全量消息
    - 追加单条或多条消息
    - 清空历史
    - 获取消息总数（jsonl 无需全量加载）
    - 格式自动检测与切换

    使用示例::

        # 自动检测格式
        mgr = HistoryFileManager(workspace_path)

        # 追加一条消息
        mgr.append_message({"role": "user", "content": "Hello"})

        # 读取最近 10 条
        recent = mgr.load_recent(10)

        # 读取全量
        all_msgs = mgr.load_all()

        # 覆盖写回
        mgr.save_all(all_msgs)
    """

    def __init__(
        self,
        workspace_path: Path,
        format: HistoryFormat = "auto",
        session_id: str = "unknown",
    ) -> None:
        """初始化 HistoryFileManager

        Args:
            workspace_path: Session 工作目录（history 文件存放于此）
            format:         文件格式（"json" | "jsonl" | "auto"）
            session_id:     会话 ID，仅用于日志标识
        """
        self.workspace_path = Path(workspace_path)
        self.session_id = session_id

        self._json_path: Path = self.workspace_path / "history.json"
        self._jsonl_path: Path = self.workspace_path / "history.jsonl"

        # 确定实际使用的格式
        if format == "auto":
            self._format: Literal["json", "jsonl"] = self._detect_format()
        elif format in ("json", "jsonl"):
            self._format = format  # type: ignore[assignment]
        else:
            raise ValueError(f"未知的 format 参数: {format!r}，应为 'json'、'jsonl' 或 'auto'")

        _rlog.info(
            f"session_{session_id}",
            f"[HistoryFileManager] 已初始化 "
            f"(format={self._format}, path={self.active_path})",
        )

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------

    @property
    def format(self) -> Literal["json", "jsonl"]:
        """当前使用的文件格式"""
        return self._format

    @property
    def active_path(self) -> Path:
        """当前格式对应的文件路径"""
        return self._jsonl_path if self._format == "jsonl" else self._json_path

    @property
    def exists(self) -> bool:
        """历史文件是否存在"""
        return self.active_path.exists()

    # ------------------------------------------------------------------
    # 核心读写接口
    # ------------------------------------------------------------------

    def load_all(self) -> List[Dict[str, Any]]:
        """读取全量历史消息

        Returns:
            消息列表（按时间顺序），若文件不存在或读取失败则返回空列表
        """
        if self._format == "jsonl":
            return self._load_all_jsonl()
        return self._load_all_json()

    def load_recent(self, count: int) -> List[Dict[str, Any]]:
        """读取最近的 N 条历史消息

        Args:
            count: 要读取的最大消息条数（>= 0；0 返回空列表）

        Returns:
            最多 count 条消息（时间升序）
        """
        if count <= 0:
            return []
        # jsonl 可优化为只读尾部（当前实现：全量加载后切片）
        all_msgs = self.load_all()
        return all_msgs[-count:] if len(all_msgs) > count else all_msgs

    def load_slice(self, start: int, end: Optional[int] = None) -> List[Dict[str, Any]]:
        """读取 [start, end) 区间的消息（基于索引，兼容 end_index 机制）

        Args:
            start: 起始索引（含，0-based）
            end:   结束索引（不含），None 表示读到末尾

        Returns:
            对应区间的消息列表
        """
        all_msgs = self.load_all()
        return all_msgs[start:end]

    def save_all(self, messages: List[Dict[str, Any]]) -> bool:
        """覆盖写入全量历史消息

        Args:
            messages: 完整的消息列表

        Returns:
            是否写入成功
        """
        if self._format == "jsonl":
            return self._save_all_jsonl(messages)
        return self._save_all_json(messages)

    def append_message(self, message: Dict[str, Any]) -> bool:
        """追加单条消息

        json 格式：读取全量 + 追加 + 整体写回（O(n) I/O）
        jsonl 格式：直接在文件末尾追加一行（O(1) I/O）

        Args:
            message: 要追加的消息 dict

        Returns:
            是否追加成功
        """
        if self._format == "jsonl":
            return self._append_jsonl(message)
        return self._append_json(message)

    def append_messages(self, messages: List[Dict[str, Any]]) -> bool:
        """批量追加多条消息

        Args:
            messages: 要追加的消息列表

        Returns:
            是否全部追加成功
        """
        if not messages:
            return True
        if self._format == "jsonl":
            return self._append_batch_jsonl(messages)
        return self._append_batch_json(messages)

    def update_message_at(self, index: int, message: Dict[str, Any]) -> bool:
        """更新指定索引处的消息（仅 json 格式高效；jsonl 需全量重写）

        Args:
            index:   0-based 绝对索引
            message: 新的消息内容

        Returns:
            是否更新成功
        """
        all_msgs = self.load_all()
        if index < 0 or index >= len(all_msgs):
            _rlog.warning(
                f"session_{self.session_id}",
                f"[HistoryFileManager] update_message_at: 索引 {index} 越界"
                f"（共 {len(all_msgs)} 条消息）",
            )
            return False
        all_msgs[index] = message
        return self.save_all(all_msgs)

    def clear(self) -> bool:
        """清空历史消息文件

        Returns:
            是否清空成功
        """
        try:
            if self._format == "jsonl":
                self._jsonl_path.write_text("", encoding="utf-8")
            else:
                self._json_path.write_text("[]", encoding="utf-8")
            _rlog.info(
                f"session_{self.session_id}",
                f"[HistoryFileManager] 历史消息已清空: {self.active_path}",
            )
            return True
        except Exception as e:
            _rlog.error(
                f"session_{self.session_id}",
                f"[HistoryFileManager] 清空历史消息失败: {e}",
            )
            return False

    def count(self) -> int:
        """返回历史消息总条数

        jsonl 格式通过逐行计数（不解析 JSON），比 load_all() 更轻量。

        Returns:
            消息总条数
        """
        if self._format == "jsonl":
            return self._count_jsonl()
        return len(self.load_all())

    def iter_messages(self) -> Iterator[Dict[str, Any]]:
        """逐条迭代历史消息（流式，适合大文件）

        Yields:
            单条消息 dict
        """
        if self._format == "jsonl":
            yield from self._iter_jsonl()
        else:
            for msg in self.load_all():
                yield msg

    # ------------------------------------------------------------------
    # 格式切换
    # ------------------------------------------------------------------

    def switch_format(
        self,
        new_format: Literal["json", "jsonl"],
        keep_old: bool = False,
    ) -> bool:
        """将历史文件转换为另一种格式

        Args:
            new_format: 目标格式 ("json" 或 "jsonl")
            keep_old:   是否保留原格式文件（默认 False，删除旧文件）

        Returns:
            是否切换成功
        """
        if new_format == self._format:
            return True  # 无需切换

        try:
            all_msgs = self.load_all()
            old_path = self.active_path

            self._format = new_format
            ok = self.save_all(all_msgs)

            if ok and not keep_old and old_path.exists():
                old_path.unlink()
                _rlog.info(
                    f"session_{self.session_id}",
                    f"[HistoryFileManager] 已删除旧格式文件: {old_path}",
                )

            _rlog.info(
                f"session_{self.session_id}",
                f"[HistoryFileManager] 格式已切换至 {new_format}: {self.active_path}",
            )
            return ok

        except Exception as e:
            _rlog.error(
                f"session_{self.session_id}",
                f"[HistoryFileManager] 格式切换失败: {e}",
            )
            return False

    # ------------------------------------------------------------------
    # 内部：格式自动检测
    # ------------------------------------------------------------------

    def _detect_format(self) -> Literal["json", "jsonl"]:
        """自动检测应使用的格式

        优先级：jsonl（若存在）> json（若存在）> json（默认）
        """
        if self._jsonl_path.exists():
            return "jsonl"
        return "json"

    # ------------------------------------------------------------------
    # 内部：JSON 格式实现
    # ------------------------------------------------------------------

    def _load_all_json(self) -> List[Dict[str, Any]]:
        """从 history.json 加载全量消息"""
        if not self._json_path.exists():
            return []
        try:
            with open(self._json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                # 兼容 {"messages": [...]} 格式
                return data.get("messages", [])
            _rlog.warning(
                f"session_{self.session_id}",
                f"[HistoryFileManager] history.json 格式非预期（非 list/dict）",
            )
            return []
        except json.JSONDecodeError as e:
            _rlog.error(
                f"session_{self.session_id}",
                f"[HistoryFileManager] history.json JSON 解析失败: {e}",
            )
            return []
        except Exception as e:
            _rlog.error(
                f"session_{self.session_id}",
                f"[HistoryFileManager] 读取 history.json 失败: {e}",
            )
            return []

    def _save_all_json(self, messages: List[Dict[str, Any]]) -> bool:
        """覆盖写入 history.json"""
        try:
            with open(self._json_path, "w", encoding="utf-8") as f:
                json.dump(messages, f, indent=2, ensure_ascii=False)
            return True
        except Exception as e:
            _rlog.error(
                f"session_{self.session_id}",
                f"[HistoryFileManager] 写入 history.json 失败: {e}",
            )
            return False

    def _append_json(self, message: Dict[str, Any]) -> bool:
        """向 history.json 追加单条消息（全量读写）"""
        all_msgs = self._load_all_json()
        all_msgs.append(message)
        return self._save_all_json(all_msgs)

    def _append_batch_json(self, messages: List[Dict[str, Any]]) -> bool:
        """向 history.json 批量追加消息（全量读写）"""
        all_msgs = self._load_all_json()
        all_msgs.extend(messages)
        return self._save_all_json(all_msgs)

    # ------------------------------------------------------------------
    # 内部：JSONL 格式实现
    # ------------------------------------------------------------------

    def _load_all_jsonl(self) -> List[Dict[str, Any]]:
        """从 history.jsonl 加载全量消息"""
        if not self._jsonl_path.exists():
            return []
        messages: List[Dict[str, Any]] = []
        try:
            with open(self._jsonl_path, "r", encoding="utf-8") as f:
                for lineno, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        messages.append(json.loads(line))
                    except json.JSONDecodeError as e:
                        _rlog.warning(
                            f"session_{self.session_id}",
                            f"[HistoryFileManager] history.jsonl 第 {lineno} 行解析失败"
                            f"（跳过）: {e}",
                        )
            return messages
        except Exception as e:
            _rlog.error(
                f"session_{self.session_id}",
                f"[HistoryFileManager] 读取 history.jsonl 失败: {e}",
            )
            return []

    def _save_all_jsonl(self, messages: List[Dict[str, Any]]) -> bool:
        """覆盖写入 history.jsonl（每条消息写一行）"""
        try:
            with open(self._jsonl_path, "w", encoding="utf-8") as f:
                for msg in messages:
                    f.write(json.dumps(msg, ensure_ascii=False) + "\n")
            return True
        except Exception as e:
            _rlog.error(
                f"session_{self.session_id}",
                f"[HistoryFileManager] 写入 history.jsonl 失败: {e}",
            )
            return False

    def _append_jsonl(self, message: Dict[str, Any]) -> bool:
        """向 history.jsonl 追加单条消息（O(1) I/O）"""
        try:
            with open(self._jsonl_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(message, ensure_ascii=False) + "\n")
            return True
        except Exception as e:
            _rlog.error(
                f"session_{self.session_id}",
                f"[HistoryFileManager] 追加消息到 history.jsonl 失败: {e}",
            )
            return False

    def _append_batch_jsonl(self, messages: List[Dict[str, Any]]) -> bool:
        """向 history.jsonl 批量追加消息（O(1) I/O）"""
        try:
            with open(self._jsonl_path, "a", encoding="utf-8") as f:
                for msg in messages:
                    f.write(json.dumps(msg, ensure_ascii=False) + "\n")
            return True
        except Exception as e:
            _rlog.error(
                f"session_{self.session_id}",
                f"[HistoryFileManager] 批量追加消息到 history.jsonl 失败: {e}",
            )
            return False

    def _count_jsonl(self) -> int:
        """统计 history.jsonl 的行数（非空行，不解析 JSON）"""
        if not self._jsonl_path.exists():
            return 0
        try:
            count = 0
            with open(self._jsonl_path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        count += 1
            return count
        except Exception as e:
            _rlog.error(
                f"session_{self.session_id}",
                f"[HistoryFileManager] 统计 history.jsonl 行数失败: {e}",
            )
            return 0

    def _iter_jsonl(self) -> Iterator[Dict[str, Any]]:
        """流式迭代 history.jsonl"""
        if not self._jsonl_path.exists():
            return
        try:
            with open(self._jsonl_path, "r", encoding="utf-8") as f:
                for lineno, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError as e:
                        _rlog.warning(
                            f"session_{self.session_id}",
                            f"[HistoryFileManager] history.jsonl 第 {lineno} 行解析失败"
                            f"（跳过）: {e}",
                        )
        except Exception as e:
            _rlog.error(
                f"session_{self.session_id}",
                f"[HistoryFileManager] 迭代 history.jsonl 失败: {e}",
            )

    # ------------------------------------------------------------------
    # 调试/信息
    # ------------------------------------------------------------------

    def get_info(self) -> Dict[str, Any]:
        """获取管理器状态信息

        Returns:
            包含格式、路径、文件大小等的信息字典
        """
        path = self.active_path
        return {
            "format": self._format,
            "active_path": str(path),
            "exists": path.exists(),
            "size_bytes": path.stat().st_size if path.exists() else 0,
            "json_path": str(self._json_path),
            "jsonl_path": str(self._jsonl_path),
            "json_exists": self._json_path.exists(),
            "jsonl_exists": self._jsonl_path.exists(),
        }

    def __repr__(self) -> str:
        return (
            f"HistoryFileManager("
            f"format={self._format!r}, "
            f"path={self.active_path!r}, "
            f"session_id={self.session_id!r})"
        )
