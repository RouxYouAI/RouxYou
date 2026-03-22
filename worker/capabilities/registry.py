import os
import glob


def list_python_files(directory="."):
    """Scan a directory for Python files, return names without .py extension."""
    python_files = glob.glob(os.path.join(directory, "*.py"))
    return [os.path.splitext(os.path.basename(f))[0]
            for f in python_files if os.path.basename(f) != "__init__.py"]
