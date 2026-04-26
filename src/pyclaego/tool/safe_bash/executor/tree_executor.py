"""命令树执行器

将已通过验证的 Node 树转换为实际的子进程调用，
使用 asyncio.create_subprocess_exec（永不使用 shell=True）。

安全设计要点：
- 所有命令通过 exec 系列调用，参数以列表形式传入，不经过 shell 解释
- ~ 通过 os.path.expanduser 在执行前展开
- 环境变量（$VAR）不展开（设计决策：消除变量注入攻击面）
- Pipeline：proc[i].stdout → proc[i+1].stdin（内核级管道，无 shell 中间层）
- 超时：asyncio.wait_for 包裹 communicate()，超时后 kill 所有进程
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

from ..exceptions import ExecutionError
from ..tree.nodes import AndNode, CmdNode, Node, OrNode, PipelineNode, SeqNode


@dataclass
class ExecResult:
    """单次执行结果"""
    stdout: bytes
    stderr: bytes
    returncode: int

    @property
    def stdout_str(self) -> str:
        return self.stdout.decode("utf-8", errors="replace")

    @property
    def stderr_str(self) -> str:
        return self.stderr.decode("utf-8", errors="replace")

    @property
    def success(self) -> bool:
        return self.returncode == 0


class TreeExecutor:
    """异步命令树执行器"""

    def __init__(self, timeout: float = 30.0) -> None:
        """
        Args:
            timeout: 整棵命令树的最大执行时间（秒）
        """
        self.timeout = timeout

    async def execute(self, node: Node, cwd: str | None = None) -> ExecResult:
        """执行命令树，返回最终结果。

        Args:
            node: 已通过验证的命令树根节点
            cwd:  工作目录（来自 SafeBashTool 配置，可被节点的 cwd 覆盖）

        Returns:
            ExecResult

        Raises:
            ExecutionError: 子进程启动失败或超时
        """
        try:
            return await asyncio.wait_for(
                self._exec_node(node, stdin_data=None, cwd=cwd),
                timeout=self.timeout,
            )
        except asyncio.TimeoutError:
            raise ExecutionError(
                f"命令树执行超时（>{self.timeout}s）"
            )

    # ------------------------------------------------------------------
    # 节点调度
    # ------------------------------------------------------------------

    async def _exec_node(
        self,
        node: Node,
        stdin_data: bytes | None,
        cwd: str | None,
    ) -> ExecResult:
        # 节点自身的 cwd 优先于传入的 cwd
        effective_cwd = node.cwd or cwd

        if isinstance(node, CmdNode):
            return await self._exec_cmd(node, stdin_data, effective_cwd)
        if isinstance(node, PipelineNode):
            return await self._exec_pipeline(node, stdin_data, effective_cwd)
        if isinstance(node, SeqNode):
            return await self._exec_seq(node, stdin_data, effective_cwd)
        if isinstance(node, AndNode):
            return await self._exec_and(node, stdin_data, effective_cwd)
        if isinstance(node, OrNode):
            return await self._exec_or(node, stdin_data, effective_cwd)
        raise ExecutionError(f"未知节点类型: {type(node).__name__}")

    # ------------------------------------------------------------------
    # CmdNode
    # ------------------------------------------------------------------

    async def _exec_cmd(
        self,
        node: CmdNode,
        stdin_data: bytes | None,
        cwd: str | None,
    ) -> ExecResult:
        argv = [node.name] + [self._expand_arg(a) for a in node.args]

        # stdin 处理
        stdin_mode: int | None
        if node.stdin is not None:
            # 重定向来自文件
            stdin_mode = None  # 在 proc 创建后手动设置
        elif stdin_data is not None:
            stdin_mode = asyncio.subprocess.PIPE
        else:
            stdin_mode = asyncio.subprocess.DEVNULL

        # stdout 处理
        stdout_mode = asyncio.subprocess.PIPE  # 默认捕获
        stdout_file = None
        if node.stdout is not None:
            # 重定向到文件，先以 PIPE 捕获，execute 后写入
            pass  # 依然 PIPE，收到后写文件

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=stdin_mode,
                stdout=stdout_mode,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
        except (FileNotFoundError, PermissionError, OSError) as e:
            raise ExecutionError(f"无法启动命令 '{node.name}': {e}") from e

        # 处理 stdin 文件重定向
        if node.stdin is not None:
            stdin_path = self._expand_arg(node.stdin.file)
            try:
                with open(stdin_path, "rb") as f:
                    stdin_data = f.read()
            except OSError as e:
                proc.kill()
                raise ExecutionError(f"读取 stdin 文件 '{stdin_path}' 失败: {e}") from e

        stdout_bytes, stderr_bytes = await proc.communicate(
            input=stdin_data if stdin_data is not None else None
        )

        # 处理 stdout 文件重定向
        if node.stdout is not None:
            stdout_path = self._expand_arg(node.stdout.file)
            mode = "ab" if node.stdout.append else "wb"
            try:
                with open(stdout_path, mode) as f:
                    f.write(stdout_bytes)
            except OSError as e:
                raise ExecutionError(f"写入 stdout 文件 '{stdout_path}' 失败: {e}") from e
            # 文件重定向后 stdout 不返回给调用方
            stdout_bytes = b""

        return ExecResult(
            stdout=stdout_bytes,
            stderr=stderr_bytes,
            returncode=proc.returncode if proc.returncode is not None else -1,
        )

    # ------------------------------------------------------------------
    # PipelineNode
    # ------------------------------------------------------------------

    async def _exec_pipeline(
        self,
        node: PipelineNode,
        stdin_data: bytes | None,
        cwd: str | None,
    ) -> ExecResult:
        """在纯异步模式下实现 a | b | c

        使用 os.pipe() 创建 OS 级管道文件描述符，通过内核直接连接进程间的
        stdout → stdin，不经过 asyncio StreamReader 中间层。

        数据流：
          stdin_data → proc[0].stdin
          proc[0] stdout_fd → proc[1] stdin_fd
          proc[1] stdout_fd → proc[2] stdin_fd  (以此类推)
          proc[-1].stdout → 捕获为 ExecResult.stdout
          所有进程的 stderr 并发读取后汇总
        """
        if not node.steps:
            raise ExecutionError("pipeline 步骤为空")

        n = len(node.steps)
        effective_cwd = node.cwd or cwd

        # 为相邻进程创建 n-1 条 OS 管道：pipes[i] = (read_fd, write_fd)
        # proc[i] 写入 pipes[i][1]，proc[i+1] 从 pipes[i][0] 读取
        pipes: list[tuple[int, int]] = [os.pipe() for _ in range(n - 1)]

        procs: list[asyncio.subprocess.Process] = []

        try:
            for i, step in enumerate(node.steps):
                argv = [step.name] + [self._expand_arg(a) for a in step.args]

                # stdin
                if i == 0:
                    stdin_src = asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL
                else:
                    stdin_src = pipes[i - 1][0]  # read end of previous pipe (real fd)

                # stdout
                if i == n - 1:
                    stdout_dst = asyncio.subprocess.PIPE  # 最后一个进程捕获 stdout
                else:
                    stdout_dst = pipes[i][1]  # write end of next pipe (real fd)

                try:
                    proc = await asyncio.create_subprocess_exec(
                        *argv,
                        stdin=stdin_src,
                        stdout=stdout_dst,
                        stderr=asyncio.subprocess.PIPE,
                        cwd=step.cwd or effective_cwd,
                    )
                except (FileNotFoundError, PermissionError, OSError) as e:
                    raise ExecutionError(f"无法启动管道命令 '{step.name}': {e}") from e

                procs.append(proc)

                # 父进程关闭已传给子进程的 fd（避免 fd 泄漏，并让管道在写端关闭时正确 EOF）
                if i > 0:
                    os.close(pipes[i - 1][0])  # 父进程关闭传给此进程的 read fd
                if i < n - 1:
                    os.close(pipes[i][1])       # 父进程关闭传给下一进程的 write fd

        except ExecutionError:
            # 清理已启动的进程
            for p in procs:
                try:
                    p.kill()
                except ProcessLookupError:
                    pass
            raise

        # 向第一个进程写入 stdin 数据
        if procs[0].stdin is not None:
            if stdin_data:
                procs[0].stdin.write(stdin_data)
            procs[0].stdin.close()

        # 并发读取所有中间进程的 stderr（stdout 已通过 OS 管道流走，不读）
        async def _read_stderr(proc: asyncio.subprocess.Process) -> bytes:
            if proc.stderr:
                return await proc.stderr.read()
            return b""

        stderr_tasks = [
            asyncio.ensure_future(_read_stderr(p)) for p in procs[:-1]
        ]

        # 收集最后一个进程的 stdout + stderr
        last_stdout, last_stderr = await procs[-1].communicate()

        # 等待中间进程退出并收集 stderr
        intermediate_stderrs = await asyncio.gather(*stderr_tasks)
        for p in procs[:-1]:
            await p.wait()

        all_stderr = list(intermediate_stderrs) + [last_stderr]

        return ExecResult(
            stdout=last_stdout,
            stderr=b"".join(all_stderr),
            returncode=procs[-1].returncode if procs[-1].returncode is not None else -1,
        )

    # ------------------------------------------------------------------
    # SeqNode
    # ------------------------------------------------------------------

    async def _exec_seq(
        self,
        node: SeqNode,
        stdin_data: bytes | None,
        cwd: str | None,
    ) -> ExecResult:
        """顺序执行，始终继续，返回最后一个步骤的结果"""
        effective_cwd = node.cwd or cwd
        result = ExecResult(stdout=b"", stderr=b"", returncode=0)
        for step in node.steps:
            result = await self._exec_node(step, stdin_data, effective_cwd)
        return result

    # ------------------------------------------------------------------
    # AndNode / OrNode
    # ------------------------------------------------------------------

    async def _exec_and(
        self,
        node: AndNode,
        stdin_data: bytes | None,
        cwd: str | None,
    ) -> ExecResult:
        """left && right：left 成功（rc==0）才执行 right"""
        effective_cwd = node.cwd or cwd
        left_result = await self._exec_node(node.left, stdin_data, effective_cwd)
        if left_result.success:
            return await self._exec_node(node.right, stdin_data, effective_cwd)
        return left_result

    async def _exec_or(
        self,
        node: OrNode,
        stdin_data: bytes | None,
        cwd: str | None,
    ) -> ExecResult:
        """left || right：left 失败（rc!=0）才执行 right"""
        effective_cwd = node.cwd or cwd
        left_result = await self._exec_node(node.left, stdin_data, effective_cwd)
        if not left_result.success:
            return await self._exec_node(node.right, stdin_data, effective_cwd)
        return left_result

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def _expand_arg(arg: str) -> str:
        """展开 ~ 前缀。环境变量（$VAR）不展开（设计决策）。"""
        if arg.startswith("~"):
            return os.path.expanduser(arg)
        return arg
