"""内置模块策略注册 — stdlib + NumPy 1.25.x + pandas 2.2.x

导入此模块时，@REGISTRY.register 装饰器自动触发所有策略的注册。
"""

from ..base_policy import ModulePolicy
from ..policy_registry import REGISTRY

# -----------------------------------------------------------------------
# Python 标准库
# -----------------------------------------------------------------------

@REGISTRY.register
class MathPolicy(ModulePolicy):
    """math 模块：所有公开数学函数均允许"""
    module_name = "math"


@REGISTRY.register
class CmathPolicy(ModulePolicy):
    """cmath 模块：复数数学函数"""
    module_name = "cmath"


@REGISTRY.register
class JsonPolicy(ModulePolicy):
    """json 模块：序列化/反序列化"""
    module_name = "json"


@REGISTRY.register
class DatetimePolicy(ModulePolicy):
    """datetime 模块：日期时间处理"""
    module_name = "datetime"


@REGISTRY.register
class RePolicy(ModulePolicy):
    """re 模块：正则表达式"""
    module_name = "re"


@REGISTRY.register
class CollectionsPolicy(ModulePolicy):
    """collections 模块：容器数据类型（Counter、defaultdict、OrderedDict 等）"""
    module_name = "collections"


@REGISTRY.register
class ItertoolsPolicy(ModulePolicy):
    """itertools 模块：高效迭代器工具"""
    module_name = "itertools"


@REGISTRY.register
class FunctoolsPolicy(ModulePolicy):
    """functools 模块：高阶函数工具（partial、reduce、lru_cache 等）"""
    module_name = "functools"


@REGISTRY.register
class StatisticsPolicy(ModulePolicy):
    """statistics 模块：统计函数（mean、stdev、median 等）"""
    module_name = "statistics"


@REGISTRY.register
class DecimalPolicy(ModulePolicy):
    """decimal 模块：高精度十进制算术"""
    module_name = "decimal"


@REGISTRY.register
class FractionsPolicy(ModulePolicy):
    """fractions 模块：有理数（Fraction）"""
    module_name = "fractions"


@REGISTRY.register
class RandomPolicy(ModulePolicy):
    """random 模块：随机数生成。

    屏蔽直接状态操作，避免对全局随机状态产生不可预期的影响。
    """
    module_name = "random"
    blocked_attributes: frozenset[str] = frozenset({
        "getstate", "setstate",  # 状态序列化/恢复，可用于重放攻击
    })


@REGISTRY.register
class StringPolicy(ModulePolicy):
    """string 模块：字符串常量和 Template"""
    module_name = "string"


@REGISTRY.register
class TextwrapPolicy(ModulePolicy):
    """textwrap 模块：文本折行与缩进"""
    module_name = "textwrap"


@REGISTRY.register
class PprintPolicy(ModulePolicy):
    """pprint 模块：美化打印"""
    module_name = "pprint"


@REGISTRY.register
class CopyPolicy(ModulePolicy):
    """copy 模块：浅复制与深复制"""
    module_name = "copy"


@REGISTRY.register
class DataclassesPolicy(ModulePolicy):
    """dataclasses 模块：数据类装饰器（dataclass、field、asdict 等）"""
    module_name = "dataclasses"


@REGISTRY.register
class EnumPolicy(ModulePolicy):
    """enum 模块：枚举类型（Enum、IntEnum、Flag 等）"""
    module_name = "enum"


@REGISTRY.register
class TypingPolicy(ModulePolicy):
    """typing 模块：类型注解。

    屏蔽 get_type_hints()，因为它会执行字符串注解（潜在代码执行路径）。
    """
    module_name = "typing"
    blocked_attributes: frozenset[str] = frozenset({
        "get_type_hints",  # 执行字符串注解，可能触发代码执行
    })


@REGISTRY.register
class AbcPolicy(ModulePolicy):
    """abc 模块：抽象基类（ABC、abstractmethod 等）"""
    module_name = "abc"


@REGISTRY.register
class HashlibPolicy(ModulePolicy):
    """hashlib 模块：哈希算法（md5、sha256 等，仅用于数据摘要）"""
    module_name = "hashlib"


@REGISTRY.register
class Base64Policy(ModulePolicy):
    """base64 模块：Base64 编解码"""
    module_name = "base64"


@REGISTRY.register
class StructPolicy(ModulePolicy):
    """struct 模块：字节序列与 C 结构体转换"""
    module_name = "struct"


@REGISTRY.register
class UuidPolicy(ModulePolicy):
    """uuid 模块：UUID 生成（uuid4 等）"""
    module_name = "uuid"
    blocked_attributes: frozenset[str] = frozenset({
        "getnode",  # 获取本机 MAC 地址（隐私信息）
    })


# -----------------------------------------------------------------------
# 第三方库：NumPy 1.25.x
# -----------------------------------------------------------------------

@REGISTRY.register
class NumpyPolicy(ModulePolicy):
    """NumPy 1.25.x：数值计算库。

    兼容版本：numpy >= 1.20, < 2.0

    文件 I/O 函数被列入黑名单：代理应通过 read_file 工具读取文件，
    再将数据传入 numpy（如 np.array(data)）。
    """
    module_name = "numpy"
    blocked_attributes: frozenset[str] = frozenset({
        # 文件 I/O
        "load", "save", "savez", "savez_compressed",
        "loadtxt", "savetxt",
        "fromfile", "tofile",
        "genfromtxt", "recfromcsv", "recfromtxt",
        # 内存映射（可映射任意文件到内存）
        "memmap",
        # 底层 C 接口（可访问任意内存）
        "ctypeslib",
    })


# -----------------------------------------------------------------------
# 第三方库：pandas 2.2.x
# -----------------------------------------------------------------------

@REGISTRY.register
class PandasPolicy(ModulePolicy):
    """pandas 2.2.x：数据分析库。

    兼容版本：pandas >= 1.5, < 3.0

    文件读取函数（read_csv、read_json 等）被允许，因为代理环境中
    通常需要直接处理数据文件。未来版本将通过 path_scope 限制可访问路径。

    SQL 读取函数被屏蔽（存在 SQL 注入风险），剪贴板访问被屏蔽（系统隐私）。
    """
    module_name = "pandas"
    blocked_attributes: frozenset[str] = frozenset({
        # SQL（存在注入风险，且需要数据库连接）
        "read_sql", "read_sql_query", "read_sql_table",
        # 系统剪贴板
        "read_clipboard",
    })
