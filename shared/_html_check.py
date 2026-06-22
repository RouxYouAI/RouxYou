"""Headless HTML load-check — run as a SUBPROCESS by shared/runtime_verify.py.

Loads a local .html file in headless chromium (Playwright), runs its scripts for
a beat, and reports uncaught page errors + console errors. Standalone so the
sync Playwright API doesn't collide with the executor's async event loop.

Usage:  python shared/_html_check.py /abs/path/to/file.html
Prints: a single JSON line {"errors": [...], "ok": bool}
"""
import sys
import json


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"errors": ["no path arg"], "ok": False})); return
    import os
    path = os.path.abspath(sys.argv[1])   # file:// requires an absolute path
    errors = []
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        # No playwright → signal "unavailable" so the caller can fall back.
        print(json.dumps({"errors": [f"playwright_unavailable: {e}"], "ok": None})); return
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox"])
            page = browser.new_page()
            page.on("pageerror", lambda exc: errors.append(f"pageerror: {str(exc)[:300]}"))

            def _console(msg):
                if msg.type in ("error", "warning"):
                    errors.append(f"console.{msg.type}: {msg.text[:300]}")
            page.on("console", _console)

            page.goto(f"file://{path}", wait_until="load", timeout=15000)
            page.wait_for_timeout(1500)  # let setInterval/rAF game loops run a beat
            browser.close()
    except Exception as e:
        errors.append(f"load_failed: {str(e)[:300]}")

    print(json.dumps({"errors": errors, "ok": len(errors) == 0}))


if __name__ == "__main__":
    main()
