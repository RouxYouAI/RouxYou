"""
Vision capability — screen capture + LLM analysis.
Optional: requires pyautogui, pillow, and a multimodal model in Ollama (e.g. minicpm-v).
"""

import os
import time
from pathlib import Path
from typing import Dict, Any

try:
    from capabilities.base import Capability
except ImportError:
    try:
        from base import Capability
    except ImportError:
        class Capability:
            pass

SCREENSHOT_DIR = Path(__file__).parent.parent / "screenshots"
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)


def capture_screen_analysis(query: str = "OCR TASK: Transcribe the text visible on screen.") -> Dict[str, Any]:
    """
    Capture the screen and analyze it with a multimodal Ollama model.
    Requires: pip install pyautogui pillow
    Requires: ollama pull minicpm-v (or any multimodal model)
    """
    try:
        import pyautogui
        import ollama

        timestamp = int(time.time())
        file_path = SCREENSHOT_DIR / f"screenshot_{timestamp}.png"

        screenshot = pyautogui.screenshot()
        screenshot.save(file_path)

        response = ollama.chat(
            model="minicpm-v",
            messages=[{"role": "user", "content": query, "images": [str(file_path)]}],
        )

        analysis = response["message"]["content"]
        return {"success": True, "screenshot_path": str(file_path),
                "analysis": analysis, "timestamp": timestamp}

    except ImportError as e:
        return {"success": False,
                "error": f"Vision not available: {e}. Run: pip install pyautogui pillow",
                "analysis": ""}
    except Exception as e:
        return {"success": False, "error": str(e), "analysis": ""}


class Vision(Capability):
    name = "vision"
    description = "Captures a screenshot and uses a multimodal model to analyze it."
    version = "1.3.0"
    dependencies = ["pyautogui", "pillow", "ollama"]

    def run(self, prompt="OCR TASK: Transcribe the visible text."):
        return capture_screen_analysis(query=prompt)
