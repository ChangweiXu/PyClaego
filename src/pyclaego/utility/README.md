# utility 模块

## 概述

`utility` 模块当前提供通用的 Session ID 校验能力，供会话创建与 WebSocket 接入前置校验复用。

---

## 文件结构

```text
utility/
├── __init__.py        # 导出 validate_session_id
└── session_utils.py   # Session ID 校验函数实现
```

---

## 导出 API

```python
from pyclaego.utility import validate_session_id
```

`__init__.py` 仅导出：

- `validate_session_id`

---

## validate_session_id

定义位置：`session_utils.py`

```python
def validate_session_id(session_id: str) -> bool:
```

### 规则

- 非空
- 必须以小写字母 `a-z` 或下划线 `_` 开头
- 后续字符仅允许：小写字母 `a-z`、数字 `0-9`、下划线 `_`

对应正则：

```python
r'^[a-z_][a-z0-9_]*$'
```

### 示例

- `sess_abc123` → `True`
- `_private_session` → `True`
- `sess-abc` → `False`（含 `-`）
- `Sess_ABC` → `False`（含大写）
- `123start` → `False`（数字开头）
- ``（空字符串）→ `False`

---

## 调用方（当前代码）

- `src/session/session.py`
  - `Session.__init__()` 中校验 `session_id`
- `src/web/websocket.py`
  - WebSocket 连接建立前校验 `session_id`

---

## 设计说明

- 该模块当前仅包含轻量校验逻辑，不涉及 I/O、配置读取或状态存储。
- 通过统一校验函数，确保 Session 相关入口使用同一规则。