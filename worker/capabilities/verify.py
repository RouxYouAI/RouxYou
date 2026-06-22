import subprocess
import os
import sys


def run_verification(file_path: str, working_dir: str = ".") -> dict:
    """
    Run a Python script and check whether it completed without errors.
    Used as the final step of most execution plans.
    """
    full_path = file_path
    if not os.path.isabs(file_path):
        full_path = os.path.join(working_dir, file_path)

    if not os.path.exists(full_path):
        if os.path.exists(file_path):
            full_path = file_path
        else:
            return {"success": False, "error": f"File not found: {full_path}", "exit_code": -1}

    try:
        result = subprocess.run(
            [sys.executable, full_path],
            capture_output=True,
            text=True,
            cwd=working_dir,
            timeout=10,
        )
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
        is_success = (result.returncode == 0) and ("Traceback" not in stderr)
        return {
            "success": is_success,
            "exit_code": result.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "verified_path": full_path,
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Execution timed out", "exit_code": -1}
    except Exception as e:
        return {"success": False, "error": str(e), "exit_code": -1}
