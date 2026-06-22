import os

def get_web_researcher_enabled() -> bool:
    return os.getenv("ROUX_WEB_RESEARCHER", "1") == "1"