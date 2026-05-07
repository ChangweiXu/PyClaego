"""Bash 执行工具 - 执行系统命令"""

import asyncio
from typing import Any

from ...logging import get_running_log
from ..base_tool import BaseTool, ToolResult, ToolStatus

_rlog = get_running_log()


class BashTool(BaseTool):
    """Bash 命令执行工具
    
    功能：
    - 执行 shell 命令
    - 支持超时控制
    - 捕获标准输出和标准错误
    - 安全限制（可配置允许/禁止的命令）
    
    配置示例：
    ```yaml
    bash:
      tool_type: "bash"
      tool_name: "bash"
      enabled: true
      timeout: 30
      allowed_commands:  # 可选：白名单模式
        - "ls"
        - "cat"
        - "grep"
      blocked_commands:  # 可选：黑名单模式
        - "rm"
        - "dd"
        - "format"
    ```
    """
    
    # 执行 shell 命令，可能修改文件系统或系统状态；并发执行存在竞争风险
    IS_READONLY: bool = False
    IS_PARALLELIZABLE: bool = False
    
    def __init__(self, tool_config: dict[str, Any]):
        """初始化 Bash 工具
        
        Args:
            tool_config: 工具配置字典
        """
        super().__init__(tool_config)
        
        # 允许的命令（白名单，为空表示不限制）
        self.allowed_commands = tool_config.get("allowed_commands", [])
        
        # 禁止的命令（黑名单）
        self.blocked_commands = tool_config.get("blocked_commands", [
            "rm", "dd", "format", "mkfs", "fdisk",
            "shutdown", "reboot", "halt", "poweroff"
        ])
        
        # 工作目录
        self.working_dir = tool_config.get("working_dir")
    
    async def execute(self, **kwargs) -> ToolResult:
        """执行 bash 命令
        
        Args:
            command: 要执行的命令字符串
            
        Returns:
            ToolResult: 执行结果
        """
        # 验证必需参数
        valid, error_msg = self.validate_params(["command"], **kwargs)
        if not valid:
            return ToolResult(status=ToolStatus.FAILED, error=error_msg)
        
        command = kwargs["command"]
        
        # 安全检查
        security_check = self._security_check(command)
        if not security_check[0]:
            return ToolResult(
                status=ToolStatus.FAILED,
                error=security_check[1]
            )
        
        try:
            _rlog.info("core_service", f"执行命令: {command}")
            
            # 异步执行命令
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.working_dir
            )
            
            # 等待命令完成（带超时）
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=self.timeout
                )
                
                # 解码输出
                stdout_str = stdout.decode('utf-8', errors='replace')
                stderr_str = stderr.decode('utf-8', errors='replace')
                
                # 检查返回码
                if process.returncode == 0:
                    _rlog.info("core_service", f"命令执行成功，返回码: {process.returncode}")
                    return ToolResult(
                        status=ToolStatus.SUCCESS,
                        output={
                            "stdout": stdout_str,
                            "stderr": stderr_str,
                            "return_code": process.returncode
                        },
                        metadata={
                            "command": command,
                            "working_dir": self.working_dir
                        }
                    )
                else:
                    _rlog.warning("core_service", f"命令执行失败，返回码: {process.returncode}")
                    return ToolResult(
                        status=ToolStatus.FAILED,
                        output={
                            "stdout": stdout_str,
                            "stderr": stderr_str,
                            "return_code": process.returncode
                        },
                        error=f"命令返回非零状态码: {process.returncode}",
                        metadata={
                            "command": command,
                            "working_dir": self.working_dir
                        }
                    )
                    
            except asyncio.TimeoutError:
                # 超时，终止进程
                process.kill()
                await process.wait()
                
                error_msg = f"命令执行超时 ({self.timeout}秒)"
                _rlog.error("core_service", error_msg)
                return ToolResult(
                    status=ToolStatus.TIMEOUT,
                    error=error_msg,
                    metadata={"command": command}
                )
                
        except Exception as e:
            error_msg = f"命令执行异常: {e!s}"
            _rlog.error("core_service", error_msg)
            return ToolResult(
                status=ToolStatus.FAILED,
                error=error_msg,
                metadata={"command": command}
            )
    
    def mask_output(self, raw_output: Any, path_mask_map: dict[str, str]) -> Any:
        """对 stdout/stderr 中的真实路径进行脱敏。

        Args:
            raw_output: execute() 返回的 output 字典，预期包含 stdout/stderr/return_code
            path_mask_map: 真实路径 -> 占位符的映射字典

        Returns:
            脱敏后的 output 字典
        """
        if not isinstance(raw_output, dict):
            return raw_output
        masked = dict(raw_output)
        if "stdout" in masked:
            masked["stdout"] = self._mask_string(masked["stdout"], path_mask_map)
        if "stderr" in masked:
            masked["stderr"] = self._mask_string(masked["stderr"], path_mask_map)
        return masked

    def _security_check(self, command: str) -> tuple[bool, str]:
        """安全检查
        
        Args:
            command: 命令字符串
            
        Returns:
            tuple[bool, str]: (是否通过, 错误信息)
        """
        # 提取命令的第一个词（实际命令）
        cmd_parts = command.strip().split()
        if not cmd_parts:
            return False, "空命令"
        
        base_cmd = cmd_parts[0]
        
        # 白名单检查（如果配置了白名单）
        if self.allowed_commands:
            if base_cmd not in self.allowed_commands:
                return False, f"命令不在允许列表中: {base_cmd}"
        
        # 黑名单检查
        if base_cmd in self.blocked_commands:
            return False, f"命令被禁止: {base_cmd}"
        
        # 检查危险模式
        dangerous_patterns = [
            "rm -rf /",
            ":(){ :|:& };:",  # fork bomb
            "chmod -R 777 /",
            "> /dev/sda"
        ]
        
        for pattern in dangerous_patterns:
            if pattern in command:
                return False, "检测到危险命令模式"
        
        return True, ""
    
    def get_description(self) -> dict[str, Any]:
        """获取工具描述
        
        Returns:
            Dict: 工具描述信息
        """
        return {
            "name": self.tool_name,
            "description": "执行 Shell 命令并返回输出结果",
            "parameters": {
                "command": {
                    "type": "string",
                    "required": True,
                    "description": "要执行的 shell 命令"
                }
            },
            "returns": {
                "stdout": "标准输出",
                "stderr": "标准错误",
                "return_code": "返回码"
            },
            "examples": [
                {
                    "command": "ls -la",
                    "description": "列出当前目录的详细内容"
                },
                {
                    "command": "cat /etc/hostname",
                    "description": "读取主机名文件"
                }
            ],
            "security": {
                "allowed_commands": self.allowed_commands if self.allowed_commands else "所有命令（除黑名单外）",
                "blocked_commands": self.blocked_commands
            },
            "is_readonly": self.__class__.IS_READONLY,
            "is_parallelizable": self.__class__.IS_PARALLELIZABLE,
        }
