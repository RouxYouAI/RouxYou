def format_bytes(num_bytes, precision=1):
    if num_bytes == 0:
        return f"0.0 B"
    
    units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
    unit_index = 0
    value = abs(num_bytes)
    
    while value >= 1024 and unit_index < len(units) - 1:
        value /= 1024
        unit_index += 1
    
    prefix = "-" if num_bytes < 0 else ""
    return f"{prefix}{value:.{precision}f} {units[unit_index]}"