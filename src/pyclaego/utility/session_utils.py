
import re


def validate_session_id(session_id: str) -> bool:
    """验证 Session ID 格式是否合法
    
    格式要求: 只能包含小写字母(a-z)、数字(0-9)和下划线(_)
    
    Args:
        session_id: 待验证的 Session ID
        
    Returns:
        bool: True 表示格式合法, False 表示不合法
        
    Examples:
        >>> validate_session_id("sess_abc123")
        True
        >>> validate_session_id("sess-abc")  # 包含短横线
        False
        >>> validate_session_id("Sess_ABC")  # 包含大写字母
        False
    """
    if not session_id:
        return False
    
    # 正则表达式: 以字母或下划线开头,后跟任意数量的字母、数字或下划线
    pattern = r'^[a-z_][a-z0-9_]*$'
    return bool(re.match(pattern, session_id))
