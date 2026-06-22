"""Behavioral smoke (tier-3, deterministic) — run as a SUBPROCESS by behavioral_verify.py.

Loads a local .html in headless chromium, then SIMULATES generic input (arrow/WASD/space/
enter keys, clicks on canvas/buttons/clickable elements) and reports errors thrown DURING
interaction — separately from load-time errors (which runtime_verify tier-2 already covers).

This catches the class of bug that loads clean but crashes on use (e.g. a buggy key handler).
It does NOT assert correct behavior ("does it do the right thing") — that's the spec-derived
assertion layer, a separate step.

Usage:  python shared/_behavioral_check.py /abs/path/file.html
Prints: one JSON line {load_errors, interaction_errors, ok, interactions}
"""
import sys
import json
import os


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"interaction_errors": ["no path arg"], "ok": False})); return
    path = os.path.abspath(sys.argv[1])
    load_errors, interaction_errors = [], []
    phase = ["load"]
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        print(json.dumps({"interaction_errors": [f"playwright_unavailable: {e}"], "ok": None})); return

    def _bucket():
        return interaction_errors if phase[0] == "interact" else load_errors

    try:
        with sync_playwright() as p:
            b = p.chromium.launch(args=["--no-sandbox"])
            pg = b.new_page()
            pg.on("pageerror", lambda exc: _bucket().append(f"pageerror: {str(exc)[:200]}"))
            pg.on("console", lambda m: _bucket().append(f"console.error: {m.text[:200]}") if m.type == "error" else None)

            pg.goto(f"file://{path}", wait_until="load", timeout=15000)
            pg.wait_for_timeout(700)            # let load/init settle (load-phase errors)

            phase[0] = "interact"
            interactions = 0
            # 1) keyboard — the breakout/pong control surface
            for k in ["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "w", "a", "s", "d", " ", "Enter"]:
                try:
                    pg.keyboard.down(k); pg.wait_for_timeout(50); pg.keyboard.up(k); pg.wait_for_timeout(50)
                    interactions += 1
                except Exception:
                    pass
            # 2) clicks — canvas, buttons, clickable cells/links
            for sel in ["canvas", "button", "[onclick]", ".cell", "[data-index]", "a"]:
                try:
                    els = pg.query_selector_all(sel)
                except Exception:
                    els = []
                for el in els[:9]:
                    try:
                        el.click(timeout=1000); pg.wait_for_timeout(40); interactions += 1
                    except Exception:
                        pass
            pg.wait_for_timeout(400)            # let any post-interaction loop tick
            b.close()
    except Exception as e:
        load_errors.append(f"load_failed: {str(e)[:200]}")
        interactions = 0

    print(json.dumps({
        "load_errors": load_errors,
        "interaction_errors": interaction_errors,
        "ok": len(interaction_errors) == 0,
        "interactions": interactions,
    }))


if __name__ == "__main__":
    main()
