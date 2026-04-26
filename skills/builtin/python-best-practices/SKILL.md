---
name: python-best-practices
description: Python 编程最佳实践指南
user-invocable: true
metadata:
  version: 1.0.0
  author: PyClaw Team
  tags: [python, coding, best-practices]
  priority: 10
  enabled: true
---

# Python 最佳实践指南

## 简介

本技能提供 Python 编程的最佳实践建议，帮助你写出更清晰、更高效、更易维护的代码。

## 类型提示

使用类型提示（Type Hints）提高代码可读性和可维护性：

```python
def greet(name: str) -> str:
    return f"Hello, {name}!"

from typing import List, Dict, Optional

def process_data(items: List[Dict[str, Any]]) -> Optional[str]:
    if not items:
        return None
    return items[0].get("name")
```

## 错误处理

使用具体的异常类型，避免捕获所有异常：

```python
# ❌ 不好的做法
try:
    result = some_operation()
except:
    pass

# ✅ 好的做法
try:
    result = some_operation()
except ValueError as e:
    logger.error(f"Invalid value: {e}")
    raise
except FileNotFoundError as e:
    logger.warning(f"File not found: {e}")
    return None
```

## 上下文管理器

使用上下文管理器自动管理资源：

```python
# ✅ 推荐
with open('file.txt', 'r') as f:
    content = f.read()

# ✅ 自定义上下文管理器
from contextlib import contextmanager

@contextmanager
def timer(name: str):
    start = time.time()
    yield
    elapsed = time.time() - start
    print(f"{name} took {elapsed:.2f}s")

with timer("My operation"):
    # 你的代码
    pass
```

## 列表推导式

使用列表推导式代替循环，代码更简洁：

```python
# ❌ 传统方式
squares = []
for i in range(10):
    squares.append(i * i)

# ✅ 列表推导式
squares = [i * i for i in range(10)]

# ✅ 带条件的推导式
even_squares = [i * i for i in range(10) if i % 2 == 0]
```

## 函数式编程

利用 Python 的函数式特性：

```python
from functools import reduce
from operator import add

# map, filter, reduce
numbers = [1, 2, 3, 4, 5]
doubled = list(map(lambda x: x * 2, numbers))
evens = list(filter(lambda x: x % 2 == 0, numbers))
total = reduce(add, numbers)

# 使用 any() 和 all()
has_negative = any(x < 0 for x in numbers)
all_positive = all(x > 0 for x in numbers)
```

## 常见陷阱

### 可变默认参数

```python
# ❌ 危险！
def add_item(item, items=[]):
    items.append(item)
    return items

# ✅ 正确做法
def add_item(item, items=None):
    if items is None:
        items = []
    items.append(item)
    return items
```

### 闭包中的变量

```python
# ❌ 错误
functions = []
for i in range(3):
    functions.append(lambda: i)
# 所有函数都返回 2

# ✅ 正确
functions = []
for i in range(3):
    functions.append(lambda x=i: x)
# 分别返回 0, 1, 2
```

## 性能优化

### 使用生成器

```python
# ❌ 占用大量内存
def get_numbers():
    return [i for i in range(1000000)]

# ✅ 节省内存
def get_numbers():
    return (i for i in range(1000000))
```

### 字符串拼接

```python
# ❌ 低效
result = ""
for s in strings:
    result += s

# ✅ 高效
result = "".join(strings)
```

## 代码风格

遵循 PEP 8 规范：

- 使用 4 个空格缩进
- 行长度不超过 79 字符
- 函数和类之间空两行
- 使用有意义的变量名
- 添加适当的注释和文档字符串

## 工具推荐

- **black**: 代码格式化工具
- **pylint**: 代码静态分析
- **mypy**: 类型检查
- **pytest**: 单元测试框架
