def parse_bool(value, default=False):
    """
    Convert common truthy/falsy spellings to a bool.
    
    Case-insensitive and whitespace-stripped:
    '1','true','yes','on','y','t' -> True
    '0','false','no','off','n','f' -> False
    
    If value is already a bool, return it unchanged.
    For None or any unrecognized string, return `default`.
    
    Args:
        value: The value to convert (str, bool, or None)
        default: Default return value for unrecognized inputs
        
    Returns:
        bool: Converted boolean value
    """
    if isinstance(value, bool):
        return value
    
    if value is None:
        return default
    
    # Convert to string and strip whitespace
    str_value = str(value).strip().lower()
    
    # Truthy values
    if str_value in ('1', 'true', 'yes', 'on', 'y', 't'):
        return True
    
    # Falsy values
    if str_value in ('0', 'false', 'no', 'off', 'n', 'f'):
        return False
    
    # Unrecognized value
    return default