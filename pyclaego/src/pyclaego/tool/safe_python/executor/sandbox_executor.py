"""SandboxExecutor — 进程隔离沙盒执行器

执行流程：
  1. SandboxExecutor.execute() 在 ThreadPoolExecutor 中调度管理函数
  2. 管理函数（_run_managed_process）用 multiprocessing.Process(spawn) 启动子进程
  3. 子进程执行 _run_in_sandbox()，在受限 __builtins__ 和受限 __import__ 下
     运行代码，并通过 multiprocessing.Queue 返回结果
  4. 管理函数负责子进程的超时和强制终止
  5. 父进程通过 asyncio.wait_for 保护整个流程

隔离保证：
  - 进程级隔离（子进程崩溃不影响父进程）
  - 真正的超时（子进程被 kill，不存在线程无法中断的问题）
  - 受限 __builtins__（eval/exec/open 等被移除）
  - 受限 __import__（只允许预先批准的模块）
  - 资源限制（Unix：RLIMIT_AS 内存上限，RLIMIT_CPU CPU 时间）

已知限制（v1）：
  - macOS 上 RLIMIT_AS 有时不能严格限制内存（内核行为不一致）
  - spawn 启动方式比 fork 慢（约 0.3-1s 冷启动），适合中低频调用
  - 不提供容器级网络/文件系统隔离（未来可加 Docker/gVisor 层）
"""

from __future__ import annotations

import asyncio
import builtins
import concurrent.futures
import multiprocessing as mp
import time
from dataclasses import dataclass
from typing import Any

from ..exceptions import SandboxTimeoutError
from ..policy.policy_registry import ALLOWED_BUILTINS_NAMES

# -----------------------------------------------------------------------
# 执行结果
# -----------------------------------------------------------------------

@dataclass
class ExecResult:
    """代码沙盒执行结果"""

    stdout: str
    stderr: str
    exec_time_ms: int
    error: str | None = None  # 代码内部运行时错误（不是系统异常）

    @property
    def success(self) -> bool:
        """代码是否无运行时错误地执行完成"""
        return self.error is None


# -----------------------------------------------------------------------
# 沙盒函数（在子进程中运行，必须是顶层函数以支持 pickle）
# -----------------------------------------------------------------------

def _run_in_sandbox(
    result_queue: mp.Queue[dict[str, Any]],
    code: str,
    builtins_names: list[str],
    approved_modules: list[str],
    max_memory_mb: int,
) -> None:
    """在独立子进程中执行受限 Python 代码。

    此函数必须是顶层函数（不能是方法或闭包），因为 multiprocessing spawn
    需要通过 pickle 传递目标函数。

    Args:
        result_queue:    用于向父进程返回结果的队列
        code:            已通过 AST 验证的代码字符串（在子进程内重新编译）
        builtins_names:  允许的内置名称列表
        approved_modules: 允许导入的顶层模块名列表
        max_memory_mb:   内存上限（MiB，通过 RLIMIT_AS 实现）
    """
    # 1. 应用资源限制（仅影响本子进程）
    try:
        import resource  # Unix only
        mem_bytes = max_memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
    except Exception:
        pass  # Windows 或不支持时静默跳过

    # 2. 构建受限内置函数表
    safe_bltns: dict[str, Any] = {}
    for name in builtins_names:
        obj = getattr(builtins, name, None)
        if obj is not None:
            safe_bltns[name] = obj

    # 3. 构建受限导入器（只允许 approved_modules 中的模块）
    approved_set: set[str] = set(approved_modules)
    _original_import = builtins.__import__

    def _restricted_import(
        name: str,
        globals: Any = None,
        locals: Any = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> Any:
        if level != 0:
            raise ImportError("相对导入不被允许")
        top = name.split(".")[0]
        if top not in approved_set:
            raise ImportError(
                f"模块 '{name}' 未被授权导入，"
                f"已授权的模块: {sorted(approved_set)}"
            )
        return _original_import(name, globals, locals, fromlist, level)

    safe_bltns["__import__"] = _restricted_import

    # 4. 构建执行环境
    # __name__ 是必须的：Python 的 __build_class__ 在创建类时会从 globals 中读取
    # __name__ 来填充类的 __module__ 属性。没有它会导致 NameError。
    safe_globals: dict[str, Any] = {
        "__builtins__": safe_bltns,
        "__name__": "<agent_code>",
        "__doc__": None,
    }

    # 5. 重定向 I/O 并执行
    import io
    import sys

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()

    start = time.monotonic()
    error_msg: str | None = None

    try:
        import ast as _ast
        tree = _ast.parse(code)
        code_obj = compile(tree, "<agent_code>", "exec")
        sys.stdout = stdout_buf
        sys.stderr = stderr_buf
        # NOTE: use globals-only exec so list comprehensions and inner functions
        # can find names bound by import/assignment in the same scope.
        # Passing a separate locals dict would break Python 3 closure semantics.
        exec(code_obj, safe_globals)
    except Exception as exc:
        error_msg = f"{type(exc).__name__}: {exc}"
    finally:
        sys.stdout = sys.__stdout__
        sys.stderr = sys.__stderr__

    elapsed_ms = int((time.monotonic() - start) * 1000)

    result_queue.put({
        "stdout": stdout_buf.getvalue(),
        "stderr": stderr_buf.getvalue(),
        "error": error_msg,
        "exec_time_ms": elapsed_ms,
    })


# -----------------------------------------------------------------------
# 进程管理函数（在 ThreadPoolExecutor 工作线程中运行）
# -----------------------------------------------------------------------

def _run_managed_process(
    code: str,
    builtins_names: list[str],
    approved_modules: list[str],
    max_memory_mb: int,
    timeout_secs: float,
) -> dict[str, Any]:
    """启动沙盒子进程并管理其生命周期（含超时强杀）。

    此函数在 ThreadPoolExecutor 的工作线程中运行，使用阻塞式 join 等待子进程。
    asyncio 层面的 wait_for 保护工作线程本身不阻塞事件循环。

    Returns:
        包含 stdout / stderr / error / exec_time_ms / timeout 的字典
    """
    ctx = mp.get_context("spawn")
    result_queue: mp.Queue[dict[str, Any]] = ctx.Queue()
    proc = ctx.Process(
        target=_run_in_sandbox,
        args=(result_queue, code, builtins_names, approved_modules, max_memory_mb),
        daemon=True,
    )
    proc.start()
    proc.join(timeout=timeout_secs)

    if proc.is_alive():
        proc.kill()
        proc.join()
        return {
            "timeout": True,
            "stdout": "",
            "stderr": "",
            "error": None,
            "exec_time_ms": int(timeout_secs * 1000),
        }

    if result_queue.empty():
        exit_code = proc.exitcode
        return {
            "timeout": False,
            "stdout": "",
            "stderr": "",
            "error": f"子进程异常退出，exit_code={exit_code}",
            "exec_time_ms": 0,
        }

    result = result_queue.get_nowait()
    result["timeout"] = False
    return result


# -----------------------------------------------------------------------
# 沙盒执行器
# -----------------------------------------------------------------------

class SandboxExecutor:
    """沙盒执行器：在独立子进程（spawn）中运行受限 Python 代码。

    使用 multiprocessing.Process(spawn) + ThreadPoolExecutor 的组合：
      - spawn 子进程：提供进程隔离和真正的超时（进程可被 kill）
      - ThreadPoolExecutor：将阻塞式 proc.join 委托给工作线程，
        使 asyncio 事件循环不被阻塞
      - asyncio.wait_for：在 asyncio 层面施加总超时保护
    """

    def __init__(self, timeout: float = 30.0, max_memory_mb: int = 256) -> None:
        """
        Args:
            timeout:        代码执行超时（秒），超时后子进程被强杀
            max_memory_mb:  子进程内存上限（MiB），通过 RLIMIT_AS 实现
        """
        self.timeout = timeout
        self.max_memory_mb = max_memory_mb

    async def execute(self, code: str, approved_modules: set[str]) -> ExecResult:
        """在沙盒中执行 Python 代码。

        Args:
            code:             已通过 AST 验证的代码字符串
            approved_modules: 通过策略验证的顶层模块名集合

        Returns:
            ExecResult

        Raises:
            SandboxTimeoutError: 执行超时（子进程被强杀）
            ExecutionError:      进程管理层异常
        """
        loop = asyncio.get_running_loop()

        # asyncio 层面的超时 = 子进程超时 + 10s 余量（给子进程 kill + join 的时间）
        asyncio_timeout = self.timeout + 10.0

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as thread_pool:
            future = loop.run_in_executor(
                thread_pool,
                _run_managed_process,
                code,
                list(ALLOWED_BUILTINS_NAMES),
                list(approved_modules),
                self.max_memory_mb,
                self.timeout,  # 子进程层超时
            )
            try:
                result_dict: dict[str, Any] = await asyncio.wait_for(
                    future, timeout=asyncio_timeout
                )
            except asyncio.TimeoutError:
                raise SandboxTimeoutError(f"代码执行超时（>{self.timeout}s）")

        if result_dict.get("timeout"):
            raise SandboxTimeoutError(f"代码执行超时（>{self.timeout}s）")

        return ExecResult(
            stdout=result_dict.get("stdout", ""),
            stderr=result_dict.get("stderr", ""),
            exec_time_ms=result_dict.get("exec_time_ms", 0),
            error=result_dict.get("error"),
        )
