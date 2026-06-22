"""
Shared path configuration for all agents
Provides a "GPS" so agents know the full project structure
"""

import os
from pathlib import Path

# Get the absolute path to the 'shared' folder
SHARED_DIR = Path(__file__).parent.absolute()

# The project root is one level up from 'shared'
PROJECT_ROOT = SHARED_DIR.parent

# Define key paths relative to root
CODER_DIR = PROJECT_ROOT / "coder"
WORKER_DIR = PROJECT_ROOT / "worker"
ORCHESTRATOR_DIR = PROJECT_ROOT / "orchestrator"
MEMORY_DIR = PROJECT_ROOT / "memory"
CONFIG_DIR = PROJECT_ROOT / "config"
LOGS_DIR = PROJECT_ROOT / "logs"
GENERATED_CODE_DIR = PROJECT_ROOT / "generated_code"
SERVICES_DIR = PROJECT_ROOT / "services"

def get_project_structure() -> str:
    """Returns a string tree of the actual project structure"""
    ignore_dirs = {'.git', '__pycache__', 'venv', '.idea', '.vscode', 'node_modules'}
    tree = []
    
    for root, dirs, files in os.walk(PROJECT_ROOT):
        # Filter out ignored directories
        dirs[:] = [d for d in dirs if d not in ignore_dirs]
        
        level = root.replace(str(PROJECT_ROOT), '').count(os.sep)
        indent = ' ' * 2 * level
        folder_name = os.path.basename(root) or 'RouxYou'
        tree.append(f"{indent}{folder_name}/")
        
        subindent = ' ' * 2 * (level + 1)
        for f in files:
            if not f.startswith('.') and not f.endswith('.pyc'):
                tree.append(f"{subindent}{f}")
    
    return "\n".join(tree[:50])  # Limit to first 50 lines

def get_agent_locations() -> dict:
    """Returns paths to all agent source files"""
    return {
        "orchestrator": ORCHESTRATOR_DIR / "orchestrator.py",
        "coder": CODER_DIR / "coder.py",
        "worker": WORKER_DIR / "worker.py",
        "memory": MEMORY_DIR / "memory_agent.py",
        "capabilities": CONFIG_DIR / "capabilities.json"
    }

# Export commonly used paths
__all__ = [
    'PROJECT_ROOT',
    'CODER_DIR',
    'WORKER_DIR',
    'ORCHESTRATOR_DIR',
    'MEMORY_DIR',
    'CONFIG_DIR',
    'GENERATED_CODE_DIR',
    'SERVICES_DIR',
    'get_project_structure',
    'get_agent_locations'
]
