"""System stats capability — CPU, memory, disk."""
import psutil
import platform


def get_system_stats() -> dict:
    system = {
        "platform":         platform.system(),
        "platform_version": platform.version(),
        "processor":        platform.processor(),
    }
    cpu = {
        "cpu_count":   psutil.cpu_count(logical=True),
        "cpu_percent": psutil.cpu_percent(interval=0.5),
    }
    mem = psutil.virtual_memory()
    memory = {
        "total_memory":     mem.total,
        "available_memory": mem.available,
        "used_memory":      mem.used,
        "memory_percent":   mem.percent,
    }
    try:
        disk_path = "/" if platform.system() != "Windows" else "C:\\"
        disk = psutil.disk_usage(disk_path)
        disk_stats = {
            "total_disk": disk.total,
            "used_disk":  disk.used,
            "free_disk":  disk.free,
        }
    except Exception:
        disk_stats = {}

    return {"system": system, "cpu": cpu, "memory": memory, "disk": disk_stats}
