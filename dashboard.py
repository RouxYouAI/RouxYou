"""
RouxYou Dashboard
Run with: streamlit run dashboard.py

Design: "Bayou" — matching rouxyou.com brand
"""

import streamlit as st
import json
import sys
import time
import requests
from pathlib import Path
from datetime import datetime
import socket
import html

# --- CONFIGURATION — all ports from config.yaml, no hardcoded values ---
BASE_DIR = Path(__file__).parent
TASKS_FILE = BASE_DIR / "tasks.json"
LOGS_DIR = BASE_DIR / "logs"
ACTIVITY_FILE = BASE_DIR / "state" / "activity.json"
REFRESH_INTERVAL = 5

if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
from config import CONFIG

ROUX_URL              = f"http://localhost:{CONFIG.PORT_ROUX}"
KILL_SWITCH_URL       = f"http://localhost:{CONFIG.PORT_WATCHTOWER_CRON}"
WATCHTOWER_DEPLOY_URL = f"http://localhost:{CONFIG.PORT_WATCHTOWER}"
WATCHTOWER_CRON_URL   = f"http://localhost:{CONFIG.PORT_WATCHTOWER_CRON}"
GATEWAY_URL           = f"http://localhost:{CONFIG.PORT_GATEWAY}"
COMPANION_URL         = f"{GATEWAY_URL}/orch/companion"
QUEUE_URL             = f"{GATEWAY_URL}/orch/queue"
HISTORY_URL           = f"{GATEWAY_URL}/orch/queue/history"

SERVICES = {
    "gateway":      CONFIG.PORT_GATEWAY,
    "orchestrator": CONFIG.PORT_ORCHESTRATOR,
    "coder":        CONFIG.PORT_CODER,
    "worker":       CONFIG.PORT_WORKER,
    "watchtower":   CONFIG.PORT_WATCHTOWER,
}

# --- PAGE CONFIG ---
st.set_page_config(
    page_title="RouxYou",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# --- ROUXYOU BAYOU THEME CSS ---
st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=DM+Sans:wght@300;400;500;600;700&family=JetBrains+Mono:wght@300;400;500&display=swap');
    :root {
        --bg-primary: #080b09; --bg-secondary: #0c110e; --bg-card: #121a15;
        --bg-input: #0a0f0b; --text-primary: #d4d0c8; --text-secondary: #9a9484;
        --text-muted: #6b5f50; --accent-pink: #c89b3c; --accent-purple: #3d6b48;
        --accent-gradient: linear-gradient(135deg, #c89b3c 0%, #3d6b48 100%);
        --success: #3d6b48; --warning: #c89b3c; --error: #8b3a3a;
        --border-subtle: rgba(42, 74, 50, 0.15);
        --moss: #2a4a32; --moss-light: #3d6b48; --bayou-blue: #1a2f3a;
        --water-blue: #2a4858; --mud: #3d2e1f; --mud-light: #6b5438;
        --cypress: #8b7a56; --gold: #c89b3c; --gold-dim: #a07d2e;
        --fog: #9a9484; --bone: #d4d0c8; --root: #5c7a4a;
    }
    .stApp { background: var(--bg-primary); font-family: 'DM Sans', sans-serif; font-weight: 300; animation: smoothFadeIn 0.3s ease-out; }
    .stApp::before { content: ''; position: fixed; inset: 0; background: radial-gradient(ellipse at 20% 50%, rgba(42, 74, 50, 0.06) 0%, transparent 60%), radial-gradient(ellipse at 80% 20%, rgba(26, 47, 58, 0.05) 0%, transparent 50%), radial-gradient(ellipse at 50% 80%, rgba(61, 46, 31, 0.04) 0%, transparent 40%); pointer-events: none; z-index: 0; }
    .stApp::after { content: ''; position: fixed; inset: 0; opacity: 0.025; background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E"); pointer-events: none; z-index: 0; }
    @keyframes smoothFadeIn { from { opacity: 0.85; } to { opacity: 1; } }
    #MainMenu {visibility: hidden;} footer {visibility: hidden;} header {visibility: hidden;}
    .main .block-container { padding: 1rem 2rem; max-width: 100%; }
    .main-header { font-family: 'Instrument Serif', serif; color: var(--gold); font-size: 2.5rem; font-weight: 400; margin-bottom: 0.3rem; letter-spacing: -0.02em; }
    .section-divider { height: 1px; background: linear-gradient(to right, var(--moss), var(--bayou-blue), transparent); margin: 1rem 0; opacity: 0.2; }
    .heartbeat-dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; background: var(--moss-light); margin-left: 0.5rem; vertical-align: middle; animation: heartbeat 2s ease-in-out infinite; }
    .heartbeat-dot.active { background: var(--gold); animation: heartbeat-active 1s ease-in-out infinite; }
    @keyframes heartbeat { 0%, 100% { opacity: 0.4; transform: scale(1); } 50% { opacity: 1; transform: scale(1.2); } }
    @keyframes heartbeat-active { 0%, 100% { opacity: 0.6; transform: scale(1); box-shadow: 0 0 4px var(--gold); } 50% { opacity: 1; transform: scale(1.4); box-shadow: 0 0 12px var(--gold); } }
    .sub-header { font-family: 'JetBrains Mono', monospace; color: var(--fog); font-size: 0.7rem; font-weight: 300; letter-spacing: 0.1em; text-transform: uppercase; margin-bottom: 1.5rem; }
    .bento-card { background: var(--bg-secondary); border: 1px solid rgba(42, 74, 50, 0.12); border-radius: 12px; padding: 1.25rem; margin-bottom: 1rem; transition: all 0.4s ease; }
    .bento-card:hover { background: rgba(42, 74, 50, 0.06); border-color: rgba(42, 74, 50, 0.25); }
    .bento-card.active { border-color: var(--gold-dim); box-shadow: 0 0 30px rgba(200, 155, 60, 0.1); }
    .card-title { font-family: 'JetBrains Mono', monospace; color: var(--fog); font-size: 0.7rem; font-weight: 400; text-transform: uppercase; letter-spacing: 0.12em; margin-bottom: 1rem; display: flex; align-items: center; gap: 0.5rem; }
    .command-center { background: linear-gradient(135deg, rgba(42, 74, 50, 0.1) 0%, rgba(200, 155, 60, 0.05) 100%); border: 2px solid var(--moss); border-radius: 16px; padding: 1.5rem; margin-bottom: 1.5rem; box-shadow: 0 0 40px rgba(42, 74, 50, 0.1); }
    .status-pill { display: inline-flex; align-items: center; gap: 0.35rem; padding: 0.3rem 0.7rem; border-radius: 9999px; font-family: 'JetBrains Mono', monospace; font-size: 0.65rem; font-weight: 400; letter-spacing: 0.03em; }
    .status-online { background: rgba(61, 107, 72, 0.15); color: var(--moss-light); border: 1px solid rgba(61, 107, 72, 0.3); }
    .status-offline { background: rgba(139, 58, 58, 0.15); color: #8b3a3a; border: 1px solid rgba(139, 58, 58, 0.3); }
    .log-viewer { background: var(--bg-input); border: 1px solid var(--border-subtle); border-radius: 8px; padding: 1rem; font-family: 'JetBrains Mono', monospace; font-size: 0.8rem; line-height: 1.6; color: var(--text-secondary); height: 350px; overflow-y: auto; white-space: pre-wrap; word-wrap: break-word; }
    .log-viewer::-webkit-scrollbar { width: 6px; } .log-viewer::-webkit-scrollbar-track { background: var(--bg-input); } .log-viewer::-webkit-scrollbar-thumb { background: var(--text-muted); border-radius: 3px; }
    .task-card { background: var(--bg-input); border: 1px solid var(--border-subtle); border-radius: 12px; padding: 1rem; margin-bottom: 0.75rem; border-left: 3px solid; transition: all 0.2s ease; }
    .task-card:hover { background: rgba(15, 23, 42, 0.8); }
    .task-pending { border-left-color: var(--warning); } .task-blocked { border-left-color: var(--accent-pink); } .task-active { border-left-color: var(--gold); } .task-completed { border-left-color: var(--success); } .task-failed { border-left-color: var(--error); }
    .task-title { color: var(--text-primary); font-weight: 500; font-size: 0.9rem; margin-bottom: 0.25rem; } .task-meta { color: var(--text-muted); font-size: 0.75rem; }
    .metric-value { font-size: 2rem; font-weight: 700; background: var(--accent-gradient); -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text; }
    .metric-label { color: var(--text-muted); font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.05em; }
    .blocked-alert { background: linear-gradient(135deg, rgba(200, 155, 60, 0.12) 0%, rgba(139, 58, 58, 0.12) 100%); border: 2px solid var(--gold-dim); border-radius: 12px; padding: 1rem; margin: 0.75rem 0; }
    .stButton > button { background: var(--accent-gradient); color: white; border: none; border-radius: 8px; padding: 0.5rem 1rem; font-weight: 500; transition: all 0.2s ease; }
    .stButton > button:hover { transform: translateY(-1px); box-shadow: 0 4px 20px rgba(200, 155, 60, 0.25); }
    .stTextInput > div > div > input { background: var(--bg-input); border: 1px solid var(--border-subtle); border-radius: 8px; color: var(--text-primary); font-family: 'DM Sans', sans-serif; }
    .stTextInput > div > div > input:focus { border-color: var(--moss-light); box-shadow: 0 0 0 2px rgba(42, 74, 50, 0.3); }
    .stTabs [data-baseweb="tab-list"] { gap: 0.5rem; background: transparent; }
    .stTabs [data-baseweb="tab"] { background: var(--bg-input); border-radius: 8px; color: var(--text-secondary); padding: 0.5rem 1rem; font-size: 0.8rem; }
    .stTabs [aria-selected="true"] { background: var(--accent-gradient); color: white; }
    .streamlit-expanderHeader { background: var(--bg-input); border-radius: 8px; color: var(--text-primary); }
    .stSelectbox > div > div { background: var(--bg-input); border-color: var(--border-subtle); font-size: 0.85rem; }
    .stCheckbox label { color: var(--text-secondary); }
    hr { border-color: var(--border-subtle); margin: 1.5rem 0; }
    .brain-activity { background: rgba(200, 155, 60, 0.03); border-left: 2px solid var(--gold-dim); border-radius: 0 8px 8px 0; padding: 1rem 1.25rem; margin-bottom: 1rem; transition: all 0.3s ease; }
    .brain-activity.active { background: rgba(200, 155, 60, 0.06); border-left: 3px solid var(--gold); box-shadow: 0 0 20px rgba(200, 155, 60, 0.08); animation: pulse-glow 2s ease-in-out infinite; }
    @keyframes pulse-glow { 0%, 100% { box-shadow: 0 0 20px rgba(200, 155, 60, 0.12); } 50% { box-shadow: 0 0 30px rgba(200, 155, 60, 0.2); } }
    .brain-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 0.75rem; }
    .brain-title { font-family: 'JetBrains Mono', monospace; color: var(--fog); font-size: 0.7rem; font-weight: 400; text-transform: uppercase; letter-spacing: 0.1em; display: flex; align-items: center; gap: 0.5rem; }
    .brain-status { font-size: 0.7rem; padding: 0.25rem 0.6rem; border-radius: 9999px; font-weight: 500; }
    .brain-status.idle { background: rgba(100, 116, 139, 0.2); color: var(--text-muted); }
    .brain-status.active { background: rgba(200, 155, 60, 0.2); color: var(--gold); }
    .brain-status.success { background: rgba(16, 185, 129, 0.2); color: var(--success); }
    .brain-status.failed { background: rgba(239, 68, 68, 0.2); color: var(--error); }
    .brain-thought { color: var(--text-secondary); font-size: 0.9rem; font-style: italic; margin-bottom: 0.75rem; padding-left: 1.5rem; border-left: 2px solid var(--gold-dim); }
    .brain-progress { display: flex; align-items: center; gap: 0.75rem; }
    .brain-progress-bar { flex: 1; height: 6px; background: rgba(100, 116, 139, 0.2); border-radius: 3px; overflow: hidden; }
    .brain-progress-fill { height: 100%; background: var(--accent-gradient); border-radius: 3px; transition: width 0.3s ease; }
    .brain-progress-text { color: var(--text-muted); font-size: 0.75rem; min-width: 60px; text-align: right; }
    .brain-task { color: var(--text-primary); font-size: 0.85rem; font-weight: 500; margin-bottom: 0.5rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .brain-agent { color: var(--text-muted); font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.03em; }
    .chat-container { background: var(--bg-card); border: 1px solid var(--border-subtle); border-radius: 12px; padding: 1rem; height: 500px; display: flex; flex-direction: column; }
    .chat-messages { flex: 1; min-height: 0; overflow-y: auto; padding-right: 0.5rem; margin-bottom: 1rem; }
    .chat-message { margin-bottom: 1rem; animation: fadeIn 0.3s ease; }
    @keyframes fadeIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }
    .chat-message.user { text-align: right; } .chat-message.assistant { text-align: left; }
    .chat-bubble { display: inline-block; max-width: 80%; padding: 0.75rem 1rem; border-radius: 16px; font-size: 0.9rem; line-height: 1.5; }
    .chat-message.user .chat-bubble { background: linear-gradient(135deg, var(--moss) 0%, var(--bayou-blue) 100%); color: var(--bone); border-bottom-right-radius: 4px; }
    .chat-message.assistant .chat-bubble { background: var(--bg-input); color: var(--text-primary); border: 1px solid var(--border-subtle); border-bottom-left-radius: 4px; }
    .chat-time { font-size: 0.65rem; color: var(--text-muted); margin-top: 0.25rem; }
    .chat-intent { font-size: 0.6rem; color: var(--gold-dim); margin-top: 0.15rem; }
    .chat-typing { color: var(--text-muted); font-style: italic; font-size: 0.85rem; }
    .proposal-card { background: var(--bg-input); border: 1px solid var(--border-subtle); border-radius: 12px; padding: 1rem 1.25rem; margin-bottom: 0.75rem; border-left: 4px solid; transition: all 0.2s ease; }
    .proposal-card:hover { background: rgba(18, 26, 21, 0.9); box-shadow: 0 0 20px rgba(42, 74, 50, 0.08); }
    .proposal-card.cat-health { border-left-color: #8b3a3a; } .proposal-card.cat-memory { border-left-color: var(--moss-light); }
    .proposal-card.cat-codebase { border-left-color: var(--water-blue); } .proposal-card.cat-tasks { border-left-color: var(--gold); }
    .proposal-card.cat-resources { border-left-color: var(--mud-light); } .proposal-card.cat-skills { border-left-color: var(--root); }
    .proposal-card.cat-optimization { border-left-color: var(--cypress); }
    .proposal-title { color: var(--text-primary); font-weight: 600; font-size: 0.9rem; margin-bottom: 0.35rem; }
    .proposal-desc { color: var(--text-secondary); font-size: 0.8rem; margin-bottom: 0.5rem; line-height: 1.4; }
    .proposal-meta { display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap; }
    .proposal-badge { display: inline-flex; align-items: center; padding: 0.2rem 0.5rem; border-radius: 6px; font-size: 0.7rem; font-weight: 500; }
    .proposal-badge.priority { background: rgba(200, 155, 60, 0.15); color: var(--gold); border: 1px solid rgba(200, 155, 60, 0.3); }
    .proposal-badge.category { background: rgba(42, 74, 50, 0.15); color: var(--moss-light); border: 1px solid rgba(42, 74, 50, 0.3); }
    .proposal-badge.reversible { background: rgba(92, 122, 74, 0.15); color: var(--root); border: 1px solid rgba(92, 122, 74, 0.3); }
    .proposal-badge.irreversible { background: rgba(139, 58, 58, 0.15); color: #8b3a3a; border: 1px solid rgba(139, 58, 58, 0.3); }
    .proposal-action { color: var(--text-muted); font-size: 0.75rem; font-family: 'JetBrains Mono', monospace; margin-top: 0.4rem; padding: 0.3rem 0.5rem; background: rgba(100, 116, 139, 0.1); border-radius: 4px; }
    .proposal-evidence { color: var(--text-muted); font-size: 0.7rem; font-style: italic; margin-top: 0.25rem; }
    .proposals-empty { text-align: center; padding: 3rem 1rem; color: var(--text-muted); }
    .proposals-empty-icon { font-size: 3rem; margin-bottom: 0.75rem; }
    .proposals-empty-text { font-size: 0.9rem; color: var(--text-secondary); }
    .proposals-empty-sub { font-size: 0.75rem; color: var(--text-muted); margin-top: 0.25rem; }
    .auto-badge { display: inline-flex; align-items: center; gap: 0.25rem; padding: 0.15rem 0.45rem; border-radius: 6px; font-size: 0.6rem; font-weight: 500; font-family: 'JetBrains Mono', monospace; }
    .auto-badge.auto-approved { background: rgba(139, 92, 246, 0.15); color: #8b5cf6; border: 1px solid rgba(139, 92, 246, 0.3); }
    .auto-badge.human-approved { background: rgba(16, 185, 129, 0.15); color: #10b981; border: 1px solid rgba(16, 185, 129, 0.3); }
</style>
""", unsafe_allow_html=True)

# --- HELPER FUNCTIONS ---
def load_tasks():
    if TASKS_FILE.exists():
        try:
            with open(TASKS_FILE, "r") as f:
                return json.load(f)
        except:
            return []
    return []

def save_tasks(tasks):
    with open(TASKS_FILE, "w") as f:
        json.dump(tasks, f, indent=2)

def check_service(port):
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        result = sock.connect_ex(("localhost", port))
        sock.close()
        return result == 0
    except:
        return False

_health_cache = {}
HEALTH_CACHE_TTL = 15

def _cached_check(name, check_fn):
    now = time.time()
    if name in _health_cache:
        ts, result = _health_cache[name]
        if now - ts < HEALTH_CACHE_TTL:
            return result
    result = check_fn()
    _health_cache[name] = (now, result)
    return result


def check_web_search():
    """
    Returns (label, status) for the web search pill.
    Status is True (green), False (red), or None (greyed out).

    - provider=none     → ("Web Search", None)   greyed out
    - provider=ddg      → check library install    green/red
    - provider=searxng  → check connectivity       green/red
    """
    provider = CONFIG.SEARCH_PROVIDER.lower()

    if provider == "none" or not provider:
        return "Web Search", None

    if provider == "duckduckgo":
        def _check_ddg():
            try:
                import duckduckgo_search  # noqa
                return True
            except ImportError:
                return False
        status = _cached_check("ddg", _check_ddg)
        return "DDG Search", status

    if provider == "searxng":
        if not CONFIG.SEARXNG_URL:
            return "SearXNG", None  # configured as provider but URL missing
        def _check_searxng():
            try:
                r = requests.get(f"{CONFIG.SEARXNG_URL}/search",
                                 params={"q": "test", "format": "json"}, timeout=3)
                return r.status_code == 200
            except:
                return False
        status = _cached_check("searxng", _check_searxng)
        return "SearXNG", status

    return f"Search ({provider})", None


def read_log(service_name, lines=100):
    log_file = LOGS_DIR / f"{service_name}.log"
    if not log_file.exists():
        return f"Waiting for {service_name} logs..."
    try:
        with open(log_file, 'r', encoding='utf-8', errors='replace') as f:
            all_lines = f.readlines()
            return ''.join(all_lines[-lines:]) if all_lines else f"Log empty for {service_name}"
    except Exception as e:
        return f"Error: {e}"

def get_activity():
    if ACTIVITY_FILE.exists():
        try:
            with open(ACTIVITY_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return {"task_id": None, "task_title": None, "agent": None, "status": "idle",
            "step": 0, "total_steps": 0, "thought": "System idle. Waiting for tasks...", "plan": []}

def render_brain_activity():
    activity = get_activity()
    status = activity.get("status", "idle")
    thought = activity.get("thought", "System idle.")
    task_title = activity.get("task_title", "")
    agent = activity.get("agent", "")
    step = activity.get("step", 0)
    total_steps = activity.get("total_steps", 0)
    progress_pct = (step / total_steps * 100) if total_steps > 0 else 0
    status_class = status if status in ["idle", "active", "success", "failed"] else "active"
    status_text = {"idle": "IDLE", "active": "THINKING", "success": "COMPLETE", "failed": "FAILED"}.get(status, "WORKING")
    thought_safe = html.escape(thought)
    task_safe = html.escape(task_title) if task_title else ""
    panel_class = "brain-activity active" if status not in ["idle", "success"] else "brain-activity"
    progress_html = f'<div class="brain-progress"><div class="brain-progress-bar"><div class="brain-progress-fill" style="width: {progress_pct}%"></div></div><span class="brain-progress-text">Step {step}/{total_steps}</span></div>' if total_steps > 0 else ""
    task_html = f'<div class="brain-task">📋 {task_safe}</div>' if task_title else ""
    agent_html = f'<div class="brain-agent">Agent: {agent}</div>' if agent else ""
    st.markdown(f'<div class="{panel_class}"><div class="brain-header"><span class="brain-title">🧠 Brain Activity</span><span class="brain-status {status_class}">{status_text}</span></div>{task_html}{agent_html}<div class="brain-thought">💭 "{thought_safe}"</div>{progress_html}</div>', unsafe_allow_html=True)

def _render_roux_controls():
    try:
        r = requests.get(f"{ROUX_URL}/voice/vad/status", timeout=1.5)
        if r.status_code != 200:
            return
        vad = r.json()
    except:
        return

    vad_active  = vad.get("vad_active", False)
    vad_awake   = vad.get("vad_awake", False)
    is_speaking = vad.get("is_speaking", False)

    if not vad_active:
        rc1, rc2 = st.columns([6, 1])
        with rc1:
            st.markdown('<span class="status-pill status-offline">🔇 Roux — VAD off</span>', unsafe_allow_html=True)
        with rc2:
            if st.button("▶ Start VAD", key="roux_vad_start", help="Start always-listening loop"):
                try: requests.post(f"{ROUX_URL}/voice/vad/toggle", timeout=3)
                except: pass
                st.rerun()
        return

    if is_speaking:
        state_label, state_style = "🔊 Speaking", "background:rgba(200,155,60,0.2);color:#c89b3c;border:1px solid rgba(200,155,60,0.4)"
    elif vad_awake:
        state_label, state_style = "👂 Listening", "background:rgba(61,107,72,0.2);color:#3d6b48;border:1px solid rgba(61,107,72,0.4)"
    else:
        state_label, state_style = "😴 Sleeping", "background:rgba(100,116,139,0.15);color:#6b7280;border:1px solid rgba(100,116,139,0.25)"

    rc1, rc2, rc3, rc4 = st.columns([3, 1, 1, 1])
    with rc1:
        st.markdown(f'<span class="status-pill" style="{state_style}">🎙️ Roux &nbsp;·&nbsp; {state_label}</span>', unsafe_allow_html=True)
    with rc2:
        if not vad_awake:
            if st.button("👂 Wake", key="roux_wake", use_container_width=True):
                try: requests.post(f"{ROUX_URL}/voice/vad/wake", timeout=3)
                except: pass
                st.rerun()
        else:
            if st.button("😴 Sleep", key="roux_sleep", use_container_width=True):
                try: requests.post(f"{ROUX_URL}/voice/vad/sleep", timeout=3)
                except: pass
                st.rerun()
    with rc3:
        if st.button("🔇 Stop", key="roux_vad_stop", use_container_width=True):
            try: requests.post(f"{ROUX_URL}/voice/vad/toggle", timeout=3)
            except: pass
            st.rerun()
    with rc4:
        st.markdown(f'<span style="font-family:JetBrains Mono,monospace;font-size:0.6rem;color:#6b5f50">wake: \'{vad.get("wake_word","roux")}\' · {vad.get("awake_timeout_s",30)}s</span>', unsafe_allow_html=True)

def _render_kill_switch():
    try:
        r = requests.get(f"{KILL_SWITCH_URL}/kill-switch", timeout=2)
        if r.status_code != 200:
            return
        state = r.json()
    except:
        return
    engaged = state.get("engaged", False)
    if engaged:
        reason = state.get("reason", "Manual")
        engaged_by = state.get("engaged_by", "unknown")
        duration = time.time() - state.get("engaged_at", time.time())
        dur_str = f"{duration:.0f}s" if duration < 60 else f"{duration/60:.0f}m"
        st.markdown(f'<div style="background:rgba(139,58,58,0.2);border:2px solid #8b3a3a;border-radius:12px;padding:0.75rem 1rem;margin-bottom:1rem"><span style="font-size:1.1rem;font-weight:700;color:#ef4444">\U0001f6d1 KILL SWITCH ENGAGED</span><br><span style="color:#94a3b8;font-size:0.8rem">{reason} (by {engaged_by}, {dur_str} ago) — Task queue frozen, auto-approve disabled</span></div>', unsafe_allow_html=True)
        if st.button("✅ Disengage Kill Switch", key="kill_switch_off"):
            try: requests.post(f"{KILL_SWITCH_URL}/kill-switch/disengage", timeout=3)
            except: pass
            st.rerun()
    else:
        ks_col1, ks_col2 = st.columns([6, 1])
        with ks_col2:
            if st.button("🛑 Kill Switch", key="kill_switch_on", help="Emergency stop: freeze all autonomous execution"):
                try: requests.post(f"{KILL_SWITCH_URL}/kill-switch/engage", json={"reason": "Dashboard manual engage", "engaged_by": "human"}, timeout=3)
                except: pass
                st.rerun()

def _render_budget_indicator():
    try:
        r = requests.get(f"{KILL_SWITCH_URL}/budget", timeout=2)
        if r.status_code != 200: return
        budget = r.json()
    except: return
    if not budget.get("enabled", True): return
    used = budget.get("used_this_hour", 0)
    max_h = budget.get("max_per_hour", 20)
    remaining = budget.get("remaining", max_h)
    resets_in = budget.get("window_resets_in", 0)
    pct = min(100, (used / max(max_h, 1)) * 100)
    bar_color = "#3d6b48" if pct < 50 else ("#c89b3c" if pct < 80 else "#8b3a3a")
    reset_text = f" — resets in {resets_in//60}m {resets_in%60}s" if resets_in > 0 and remaining == 0 else ""
    st.markdown(f'<div style="display:flex;align-items:center;gap:0.75rem;margin-bottom:0.75rem"><span style="font-family:JetBrains Mono,monospace;font-size:0.7rem;color:var(--fog);text-transform:uppercase;letter-spacing:0.08em;min-width:130px">\U0001f4ca Budget: {used}/{max_h}/hr</span><div style="flex:1;height:6px;background:rgba(100,116,139,0.15);border-radius:3px;overflow:hidden"><div style="width:{pct}%;height:100%;background:{bar_color};border-radius:3px;transition:width 0.3s ease"></div></div><span style="font-family:JetBrains Mono,monospace;font-size:0.65rem;color:var(--text-muted);min-width:80px;text-align:right">{remaining} remaining{reset_text}</span></div>', unsafe_allow_html=True)

def fetch_pending_deploys():
    try:
        r = requests.get(f"{WATCHTOWER_DEPLOY_URL}/deploy/pending", timeout=3)
        if r.status_code == 200:
            return r.json().get("pending", [])
    except: pass
    return []

def render_deploy_panel():
    pending = fetch_pending_deploys()
    if not pending: return
    for deploy in pending:
        deploy_id = deploy.get("deploy_id", "")
        service = deploy.get("service", "unknown")
        version = deploy.get("version", "?")
        staging_port = deploy.get("staging_port", 0)
        health = deploy.get("health_result", {})
        latency = health.get("latency_ms", "?") if health else "?"
        patch_count = len(deploy.get("patches", []))
        created = deploy.get("created_at", 0)
        time_str = datetime.fromtimestamp(created).strftime("%I:%M:%S %p") if created else ""
        st.markdown(f'<div class="blocked-alert"><span style="font-size:1.1rem;font-weight:600;color:#f8fafc">🚀 Deploy Awaiting Approval</span><br><span style="color:#94a3b8;font-size:0.85rem"><b>{service}</b> v{version} • Staging :{staging_port} • Health ✅ {latency}ms • {patch_count} patch(es) • {time_str}</span><div style="font-family:monospace;font-size:0.7rem;color:#64748b;margin-top:0.5rem">{deploy_id}</div></div>', unsafe_allow_html=True)
        btn_cols = st.columns([1, 1, 4])
        with btn_cols[0]:
            if st.button("✅ Approve & Swap", key=f"approve_{deploy_id}", use_container_width=True):
                try:
                    r = requests.post(f"{WATCHTOWER_DEPLOY_URL}/deploy/approve/{deploy_id}", timeout=30)
                    result = r.json()
                    if result.get("success"): st.success(f"🎉 {result.get('message', 'Deploy complete!')}")
                    else: st.error(f"❌ {result.get('error', 'Swap failed')}")
                except Exception as e: st.error(f"Error: {e}")
                st.rerun()
        with btn_cols[1]:
            if st.button("❌ Reject", key=f"reject_{deploy_id}", use_container_width=True):
                try:
                    r = requests.post(f"{WATCHTOWER_DEPLOY_URL}/deploy/reject/{deploy_id}", timeout=10)
                    result = r.json()
                    if result.get("success"): st.info(f"🚫 {result.get('message', 'Rejected.')}")
                    else: st.error(f"❌ {result.get('error', 'Failed')}")
                except Exception as e: st.error(f"Error: {e}")
                st.rerun()

def format_time(ts):
    try: return datetime.fromtimestamp(ts).strftime("%H:%M")
    except: return ""

# --- CONVERSATION FUNCTIONS ---
from shared.conversations import (
    create_conversation, get_active_conversation_id, set_active_conversation,
    list_conversations, search_conversations, delete_conversation, pin_conversation,
    add_message as conv_add_message, get_messages as conv_get_messages,
    generate_title as conv_generate_title, update_title as conv_update_title,
    migrate_existing_history,
)
migrate_existing_history()

def send_to_companion(message, confirmed=False, priority="normal"):
    try:
        payload = {"message": message, "priority": priority}
        if confirmed: payload["confirmed"] = True
        response = requests.post(COMPANION_URL, json=payload, timeout=180)
        if response.status_code == 200: return response.json()
        return {"success": False, "response": f"Error: {response.status_code}"}
    except requests.exceptions.ConnectionError:
        return {"success": False, "response": "🔴 Gateway/Orchestrator offline. Start the agents first!"}
    except Exception as e:
        return {"success": False, "response": f"Error: {str(e)}"}

def _on_conv_select_change():
    if st.session_state.get("_force_conv"): return
    selected_label = st.session_state.get("conv_select")
    if selected_label is None: return
    conv_map = st.session_state.get("_conv_label_map", {})
    selected_id = conv_map.get(selected_label)
    if selected_id: set_active_conversation(selected_id)

def render_conversation_bar():
    convs = list_conversations(limit=30)
    active_id = get_active_conversation_id()
    conv_options = [c["id"] for c in convs] if convs else []
    conv_labels = []
    for c in convs:
        pin = "📌 " if c.get("pinned") else ""
        ts = datetime.fromtimestamp(c.get("updated_at", 0)).strftime("%b %d, %I:%M %p") if c.get("updated_at") else ""
        count = c.get("msg_count", 0)
        conv_labels.append(f"{pin}{c.get('title', 'Untitled')[:45]}  •  {ts}  ({count} msgs)")
    label_map = dict(zip(conv_labels, conv_options))
    st.session_state["_conv_label_map"] = label_map
    c1, c2, c3, c4 = st.columns([1, 4, 0.5, 0.5])
    with c1:
        if st.button("➕ New Chat", use_container_width=True, key="new_chat"):
            new_id = create_conversation()
            set_active_conversation(new_id)
            st.session_state["_force_conv"] = new_id
            if "conv_select" in st.session_state: del st.session_state["conv_select"]
            st.rerun()
    with c2:
        if conv_options:
            forced_id = st.session_state.pop("_force_conv", None)
            current_idx = conv_options.index(forced_id) if forced_id and forced_id in conv_options else (conv_options.index(active_id) if active_id in conv_options else 0)
            st.selectbox("Conversation", options=conv_labels, index=current_idx, label_visibility="collapsed", key="conv_select", on_change=_on_conv_select_change)
    with c3:
        if active_id and st.button("📌", use_container_width=True, key="pin_conv"):
            current_conv = next((c for c in convs if c["id"] == active_id), None)
            if current_conv: pin_conversation(active_id, not current_conv.get("pinned", False)); st.rerun()
    with c4:
        if active_id and st.button("🗑️", use_container_width=True, key="delete_conv"):
            delete_conversation(active_id)
            remaining = list_conversations(limit=1)
            if remaining: set_active_conversation(remaining[0]["id"]); st.session_state["_force_conv"] = remaining[0]["id"]
            else: new_id = create_conversation(); st.session_state["_force_conv"] = new_id
            st.rerun()

def render_chat_panel():
    st.markdown('<div class="card-title">💬 Companion Chat</div>', unsafe_allow_html=True)
    render_conversation_bar()
    if "pending_confirmation" not in st.session_state:
        st.session_state.pending_confirmation = None
    active_id = get_active_conversation_id()

    if _FRAGMENT_AVAILABLE:
        _chat_messages_fragment()
    else:
        history = conv_get_messages(limit=50, conv_id=active_id)
        awaiting = st.session_state.get("_awaiting_task")
        task_is_running = False
        if awaiting:
            task_id = awaiting["task_id"]
            try:
                r = requests.get(f"{GATEWAY_URL}/orch/queue/{task_id}", timeout=3)
                if r.status_code == 200:
                    task_state = r.json().get("state", "")
                    if task_state in ("completed", "failed", "cancelled"):
                        del st.session_state["_awaiting_task"]; st.rerun()
                    elif task_state in ("queued", "running"):
                        task_is_running = True
                elif r.status_code == 404:
                    del st.session_state["_awaiting_task"]; st.rerun()
            except: pass
            if time.time() - awaiting.get("queued_at", 0) > 300:
                st.session_state.pop("_awaiting_task", None); task_is_running = False

        chat_html = '<div class="chat-container"><div class="chat-messages" id="chat-scroll">'
        if not history:
            chat_html += '<div class="chat-message assistant"><div class="chat-bubble">👋 I\'m your Companion. Ask me anything or tell me what to do!</div></div>'
        for msg in history:
            role = msg.get("role", "user")
            content = html.escape(msg.get("content", "")).replace("\n", "<br>")
            ts = msg.get("timestamp", 0)
            time_str = datetime.fromtimestamp(ts).strftime("%H:%M") if ts else ""
            intent = msg.get("metadata", {}).get("intent", "")
            intent_badge = f'<div class="chat-intent">{intent}</div>' if intent and role == "assistant" else ""
            chat_html += f'<div class="chat-message {role}"><div class="chat-bubble">{content}</div><div class="chat-time">{time_str}</div>{intent_badge}</div>'
        if task_is_running:
            elapsed = time.time() - awaiting.get("queued_at", time.time())
            elapsed_str = f"{elapsed:.0f}s" if elapsed < 60 else f"{elapsed/60:.1f}m"
            chat_html += f'<div class="chat-message assistant"><div class="chat-bubble" style="opacity:0.7"><span class="chat-typing">Working on it ({elapsed_str})</span></div></div>'
        chat_html += '</div></div>'
        st.markdown(chat_html, unsafe_allow_html=True)
        import streamlit.components.v1 as components
        components.html('<script>var c=window.parent.document.getElementById("chat-scroll");if(c)c.scrollTop=c.scrollHeight;</script>', height=0)

    if st.session_state.pending_confirmation:
        pending = st.session_state.pending_confirmation
        conf_cols = st.columns([1, 1, 3])
        with conf_cols[0]:
            if st.button("✅ Yes, proceed", key="confirm_yes", use_container_width=True):
                with st.spinner("⚡ Executing..."):
                    result = send_to_companion(pending["original_request"], confirmed=True)
                st.session_state.pending_confirmation = None; st.rerun()
        with conf_cols[1]:
            if st.button("❌ Cancel", key="confirm_no", use_container_width=True):
                conv_add_message("user", "No, cancel", conv_id=active_id)
                conv_add_message("assistant", "Got it, cancelled. 👍", conv_id=active_id)
                st.session_state.pending_confirmation = None; st.rerun()

    p_col1, p_col2 = st.columns([6, 2])
    with p_col2:
        priority_choice = st.selectbox("Priority", ["🟡 Normal", "⚡ Urgent", "⚪ Background"], index=0, label_visibility="collapsed", key="priority_select")
    selected_priority = {"🟡 Normal": "normal", "⚡ Urgent": "urgent", "⚪ Background": "background"}.get(priority_choice, "normal")

    with st.form("chat_form", clear_on_submit=True):
        col1, col2 = st.columns([6, 1])
        with col1:
            chat_input = st.text_input("Message", placeholder="Ask questions, give commands, or just chat...", label_visibility="collapsed", key="chat_input")
        with col2:
            send_clicked = st.form_submit_button("📨 Send", use_container_width=True)

    if send_clicked and chat_input:
        with st.spinner("🤔 Thinking..."):
            result = send_to_companion(chat_input, priority=selected_priority)
        if result.get("needs_confirmation"):
            st.session_state.pending_confirmation = {"original_request": result.get("original_request", chat_input)}
        if result.get("queued") and result.get("task_id"):
            st.session_state["_awaiting_task"] = {"task_id": result["task_id"], "query": chat_input, "queued_at": time.time()}
        msg_count = len([m for m in conv_get_messages(limit=100, conv_id=active_id) if m["role"] == "user"])
        if msg_count == 3:
            import asyncio
            try:
                loop = asyncio.new_event_loop()
                loop.run_until_complete(conv_generate_title(active_id))
                loop.close()
            except Exception: pass
        st.rerun()

def _priority_badge(label):
    return {"urgent": "🔴", "normal": "🟡", "background": "⚪"}.get(label or "", "🟡")

def _fmt_duration(started, completed):
    if started and completed:
        d = completed - started
        return f"{d:.1f}s" if d < 60 else f"{d/60:.1f}m"
    return ""

def _fmt_timestamp(ts):
    try: return datetime.fromtimestamp(ts).strftime("%b %d, %I:%M %p")
    except: return ""

def render_queue_panel():
    qs = None
    try:
        r = requests.get(QUEUE_URL, timeout=3)
        if r.status_code == 200: qs = r.json()
    except: pass
    if qs is None:
        st.warning("🔴 Cannot reach Orchestrator queue. Is it running?")
        return
    stats = qs.get("stats", {})
    current = qs.get("current")
    pending = qs.get("pending", [])
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Queued", stats.get("pending_count", 0))
    is_paused = stats.get("is_paused", False)
    m2.metric("Processing", "⏸️ Paused" if is_paused else ("▶️ Active" if stats.get("is_processing") else "— Idle"))
    m3.metric("Completed", stats.get("total_completed", 0))
    m4.metric("Failed", stats.get("total_failed", 0))
    ctrl1, ctrl2, ctrl3 = st.columns([1, 1, 4])
    with ctrl1:
        if is_paused:
            if st.button("▶️ Resume", use_container_width=True, key="q_resume"):
                try: requests.post(f"{GATEWAY_URL}/orch/queue/resume", timeout=3)
                except: pass
                st.rerun()
        else:
            if st.button("⏸️ Pause", use_container_width=True, key="q_pause"):
                try: requests.post(f"{GATEWAY_URL}/orch/queue/pause", timeout=3)
                except: pass
                st.rerun()
    with ctrl2:
        if current and st.button("🛑 Abort Task", use_container_width=True, key="q_abort"):
            try: requests.post(f"{GATEWAY_URL}/orch/queue/abort", timeout=3)
            except: pass
            st.rerun()
    if current:
        elapsed = time.time() - current.get("started_at", time.time())
        badge = _priority_badge(current.get("priority_label"))
        st.markdown(f'<div class="bento-card active"><span class="card-title">▶️ Running — {elapsed:.0f}s</span><div style="margin-top:0.5rem">{badge} <code>#{current["id"]}</code> — {html.escape(current["query"][:120])}</div></div>', unsafe_allow_html=True)
    if pending:
        st.markdown(f"**⏳ Pending ({len(pending)})**")
        for task in pending:
            c1, c2 = st.columns([6, 1])
            with c1:
                age = time.time() - task.get("created_at", time.time())
                age_str = f"{age:.0f}s ago" if age < 60 else f"{age/60:.0f}m ago"
                st.markdown(f"{_priority_badge(task.get('priority_label'))} `#{task['id']}` — {task['query'][:70]}")
                st.caption(f"Queued {age_str}")
            with c2:
                if st.button("🗑️", key=f"cancel_{task['id']}", help="Cancel task"):
                    try: requests.delete(f"{QUEUE_URL}/{task['id']}", timeout=3)
                    except: pass
                    st.rerun()
    elif not current:
        st.info("✨ Queue empty. Send tasks via the Chat tab!")
    st.markdown("---")
    hist_ctrl1, hist_ctrl2, _ = st.columns([1, 1, 4])
    with hist_ctrl1:
        hide_archived = st.toggle("🙈 Hide Archived", value=False, key="hide_archived")
    with hist_ctrl2:
        if st.button("📦 Archive All", key="archive_all"):
            try: requests.post(f"{GATEWAY_URL}/orch/queue/archive-all", timeout=5)
            except:
                try: requests.post(f"http://localhost:{CONFIG.PORT_ORCHESTRATOR}/queue/archive-all", timeout=5)
                except: pass
            st.rerun()
    full_history = []
    hist_params = {"limit": 50, "include_archived": "true"}
    if hide_archived: hist_params.pop("include_archived")
    try:
        resp = requests.get(HISTORY_URL, params=hist_params, timeout=5)
        if resp.status_code == 200: full_history = resp.json().get("tasks", [])
    except: pass
    if not full_history:
        try:
            resp = requests.get(f"http://localhost:{CONFIG.PORT_ORCHESTRATOR}/queue/history", params=hist_params, timeout=5)
            if resp.status_code == 200: full_history = resp.json().get("tasks", [])
        except: pass
    if not full_history:
        mem = qs.get("history", [])
        full_history = [t for t in mem if not t.get("archived", False)] if hide_archived else mem
    if full_history:
        st.markdown(f"**📜 Task History** ({len(full_history)} recent)")
        for task in full_history:
            state = task.get("state", "")
            is_archived = task.get("archived", False)
            emoji = {"completed": "✅", "failed": "❌", "cancelled": "🚫"}.get(state, "❓")
            badge = _priority_badge(task.get("priority_label"))
            dur = _fmt_duration(task.get("started_at"), task.get("completed_at"))
            ts = _fmt_timestamp(task.get("completed_at") or task.get("created_at"))
            archive_label = " 📦" if is_archived else ""
            with st.expander(f"{emoji} {badge} {task['query'][:60]}{'...' if len(task.get('query',''))>60 else ''}  —  {dur}  •  {ts}{archive_label}", expanded=False):
                st.caption(f"ID: `{task['id']}` • Priority: {task.get('priority_label','normal')} • State: {state}")
                st.markdown(f"**Query:** {task['query']}")
                if task.get("error"): st.error(f"Error: {task['error'][:300]}")
                result = task.get("result", {})
                if isinstance(result, dict) and result.get("synthesized_response"):
                    st.markdown(f"**Result:** {result['synthesized_response'][:500]}")
                task_id = task.get("id", "")
                if is_archived:
                    if st.button("📤 Unarchive", key=f"unarchive_{task_id}"):
                        try: requests.post(f"{GATEWAY_URL}/orch/queue/{task_id}/unarchive", timeout=5)
                        except:
                            try: requests.post(f"http://localhost:{CONFIG.PORT_ORCHESTRATOR}/queue/{task_id}/unarchive", timeout=5)
                            except: pass
                        st.rerun()
                else:
                    if st.button("📦 Archive", key=f"archive_{task_id}"):
                        try: requests.post(f"{GATEWAY_URL}/orch/queue/{task_id}/archive", timeout=5)
                        except:
                            try: requests.post(f"http://localhost:{CONFIG.PORT_ORCHESTRATOR}/queue/{task_id}/archive", timeout=5)
                            except: pass
                        st.rerun()
    else:
        st.caption("No task history yet." if not hide_archived else "No unarchived tasks. Toggle off to see all.")

def _cat_color(category):
    return {"health": "#ef4444", "memory": "#8b5cf6", "codebase": "#3b82f6", "tasks": "#f59e0b", "resources": "#f97316", "skills": "#10b981", "optimization": "#06b6d4"}.get(category, "#64748b")

def _cat_emoji(category):
    return {"health": "🏥", "memory": "🧠", "codebase": "📦", "tasks": "📋", "resources": "💾", "skills": "⚡", "optimization": "🔬"}.get(category, "❓")

def render_proposals_panel():
    import streamlit.components.v1 as components

    approved = st.session_state.pop("_proposal_approved", None)
    if approved: st.success(f"✅ {approved}")
    dismissed = st.session_state.pop("_proposal_dismissed", None)
    if dismissed: st.info(f"🗑️ Dismissed: {dismissed}. Won't re-propose for 12h.")

    if "notif_permission_asked" not in st.session_state:
        components.html('<script>if ("Notification" in window && Notification.permission === "default") { Notification.requestPermission(); }</script>', height=0)
        st.session_state["notif_permission_asked"] = True

    ctrl1, ctrl2, ctrl3, ctrl4 = st.columns([1, 1, 1.5, 2.5])
    with ctrl1:
        manual_scan = st.button("🔍 Scan Now", use_container_width=True, key="proposer_scan")
    with ctrl2:
        st.caption("🔄 Auto-scans every 30min via cron")

    _aa_config = {}
    try:
        resp = requests.get(f"{WATCHTOWER_CRON_URL}/auto-approve/config", timeout=3)
        if resp.status_code == 200: _aa_config = resp.json().get("config", {})
    except: pass

    with ctrl3:
        _aa_enabled = _aa_config.get("enabled", True)
        _aa_toggle = st.toggle("⚡ Auto-Approve", value=_aa_enabled, key="auto_approve_toggle")
        if _aa_toggle != _aa_enabled:
            try: requests.post(f"{WATCHTOWER_CRON_URL}/auto-approve/config", json={"enabled": _aa_toggle}, timeout=3)
            except: pass

    with ctrl4:
        if _aa_config:
            _daily_used = _aa_config.get("today_count", 0)
            _daily_limit = _aa_config.get("daily_limit", 10)
            new_limit = st.slider("Daily limit", 1, 50, _daily_limit, key="auto_approve_limit", label_visibility="collapsed")
            st.caption(f"🤖 {_daily_used}/{_daily_limit} auto-approved today")
            if new_limit != _daily_limit:
                try: requests.post(f"{WATCHTOWER_CRON_URL}/auto-approve/config", json={"daily_limit": new_limit}, timeout=3)
                except: pass

    data = None
    if manual_scan:
        with st.spinner("🔍 Running proposer scan (may take ~45s)..."):
            try:
                r = requests.post(f"{WATCHTOWER_CRON_URL}/proposals", timeout=120)
                if r.status_code == 200: data = r.json()
            except: pass

    if data is None:
        try:
            r = requests.get(f"{WATCHTOWER_CRON_URL}/proposals", timeout=5)
            if r.status_code == 200: data = r.json()
        except requests.exceptions.ConnectionError: pass
        except Exception as e: st.caption(f"⚠️ Error: {e}")

    if data is None:
        st.warning(f"🔴 Cannot reach Watchtower Cron (port {CONFIG.PORT_WATCHTOWER_CRON}). Start it with: python services/watchtower/api.py")
        return

    proposals = [p for p in (data.get("proposals", []) or []) if isinstance(p, dict)]
    observer_stats = data.get("observer_stats", {})

    obs_cols = st.columns(6)
    for col, name in zip(obs_cols, ["health", "memory", "codebase", "tasks", "resources", "skills"]):
        with col:
            count = observer_stats.get(name, 0)
            emoji = _cat_emoji(name)
            if isinstance(count, int) and count > 0:
                st.markdown(f'<div style="text-align:center;padding:0.3rem;background:rgba(139,58,58,0.15);border-radius:8px"><div style="font-size:1.2rem">{emoji}</div><div style="color:#f8fafc;font-size:0.75rem;font-weight:600">{count}</div><div style="color:#64748b;font-size:0.6rem;text-transform:uppercase">{name}</div></div>', unsafe_allow_html=True)
            else:
                st.markdown(f'<div style="text-align:center;padding:0.3rem;background:rgba(100,116,139,0.08);border-radius:8px"><div style="font-size:1.2rem">{emoji}</div><div style="color:#10b981;font-size:0.75rem">✓</div><div style="color:#64748b;font-size:0.6rem;text-transform:uppercase">{name}</div></div>', unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)
    if not proposals:
        st.markdown('<div class="proposals-empty"><div class="proposals-empty-icon">✅</div><div class="proposals-empty-text">System looks healthy</div><div class="proposals-empty-sub">No proposals — all observers found nothing to flag</div></div>', unsafe_allow_html=True)
        return

    st.markdown(f'<div style="color:#f8fafc;font-weight:600;font-size:0.9rem;margin-bottom:0.75rem">🔔 {len(proposals)} Proposal{"s" if len(proposals) != 1 else ""}</div>', unsafe_allow_html=True)

    for i, p in enumerate(proposals):
        cat = p.get("category", "")
        priority = p.get("priority", 0)
        reversible = p.get("reversible", True)
        title = html.escape(p.get("title", ""))
        desc = html.escape(p.get("description", ""))
        action = html.escape(p.get("proposed_action", ""))
        evidence = html.escape(p.get("evidence", ""))
        state = p.get("state", "pending")
        source = p.get("source", "heuristic")
        executor_label = p.get("executor", "manual")
        rev_class = "reversible" if reversible else "irreversible"
        rev_label = "↩ reversible" if reversible else "⚠ irreversible"
        state_colors = {"pending": ("#f59e0b", "⏳"), "approved": ("#8b5cf6", "✅"), "executing": ("#3b82f6", "⚡"), "completed": ("#10b981", "✔️"), "failed": ("#ef4444", "❌"), "dismissed": ("#64748b", "🚫")}
        s_color, s_icon = state_colors.get(state, ("#64748b", "❓"))
        confidence = p.get("confidence", None)
        coach_reasoning = p.get("coach_reasoning", "")
        conf_html = ""
        if confidence is not None and source == "coach":
            conf_pct = int(confidence * 100)
            conf_color = "#10b981" if confidence > 0.7 else ("#f59e0b" if confidence > 0.4 else "#ef4444")
            conf_html = f'<span style="font-size:0.65rem;padding:0.15rem 0.4rem;border-radius:4px;background:{conf_color}22;color:{conf_color};border:1px solid {conf_color}44;margin-left:0.25rem">🎯 {conf_pct}%</span>'
        source_html = '<span style="font-size:0.6rem;padding:0.12rem 0.35rem;border-radius:4px;background:rgba(200,155,60,0.12);color:#c89b3c;border:1px solid rgba(200,155,60,0.25);margin-left:0.25rem;font-family:JetBrains Mono,monospace">🧠 Coach</span>' if source == "coach" else ('                <span style="font-size:0.6rem;padding:0.12rem 0.35rem;border-radius:4px;background:rgba(42,72,88,0.2);color:#2a4858;border:1px solid rgba(42,72,88,0.3);margin-left:0.25rem;font-family:JetBrains Mono,monospace">🔬 Research</span>' if source == "research" else "")
        approved_by = p.get("approved_by", "")
        approved_html = '<span class="auto-badge auto-approved" style="margin-left:0.25rem">🤖 Auto</span>' if approved_by == "auto" else ('<span class="auto-badge human-approved" style="margin-left:0.25rem">👤 Human</span>' if approved_by == "human" else "")
        reasoning_html = f'<div style="color:var(--gold-dim);font-size:0.7rem;font-style:italic;margin-top:0.25rem;padding-left:0.5rem;border-left:2px solid rgba(200,155,60,0.3)">🧠 {html.escape(coach_reasoning)}</div>' if coach_reasoning else ""
        st.markdown(f'<div class="proposal-card cat-{cat}"><div style="display:flex;justify-content:space-between;align-items:center"><div class="proposal-title">{_cat_emoji(cat)} {title}</div><div>{source_html}{conf_html}{approved_html}<span style="font-size:0.65rem;padding:0.15rem 0.4rem;border-radius:4px;background:{s_color}22;color:{s_color};border:1px solid {s_color}44;margin-left:0.25rem">{s_icon} {state}</span></div></div><div class="proposal-desc">{desc}</div>{reasoning_html}<div class="proposal-meta"><span class="proposal-badge priority">P{priority}</span><span class="proposal-badge category">{cat}</span><span class="proposal-badge {rev_class}">{rev_label}</span><span style="font-size:0.65rem;color:#64748b">→ {executor_label}</span></div><div class="proposal-action">→ {action}</div><div class="proposal-evidence">Evidence: {evidence}</div></div>', unsafe_allow_html=True)

        prop_id = p.get("id", f"unknown_{i}")
        executor = p.get("executor", "manual")
        if state != "pending": continue
        btn1, btn2, btn3 = st.columns([1, 1, 4])
        with btn1:
            if st.button("✅ Approve", key=f"approve_{prop_id}", use_container_width=True):
                try: requests.post(f"{WATCHTOWER_CRON_URL}/proposals/{prop_id}/approve", timeout=5)
                except: pass
                orch_payload = {"proposal_id": prop_id, "title": p.get("title",""), "description": p.get("description",""), "category": cat, "priority": priority, "proposed_action": p.get("proposed_action",""), "executor": executor, "executor_meta": p.get("executor_meta",{})}
                spinner_msg = {"watchtower": "🔄 Dispatching restart...", "coder": "🧠 Queuing for Coder...", "worker": "⚙️ Queuing for Worker...", "manual": "📝 Acknowledging..."}.get(executor, "⚡ Processing...")
                with st.spinner(spinner_msg):
                    try:
                        r = requests.post(f"{GATEWAY_URL}/orch/queue/proposal", json=orch_payload, timeout=30)
                        result = r.json()
                        if result.get("success"):
                            if result.get("queued"): st.session_state["_proposal_approved"] = f"Queued as task #{result.get('task_id','?')} ({result.get('priority','?')} priority)"
                            elif result.get("state") == "completed":
                                r_data = result.get("result", {})
                                detail = r_data.get("message", "") if isinstance(r_data, dict) else ""
                                affected = r_data.get("affected", []) if isinstance(r_data, dict) else []
                                parts = [f"Done: {detail}"] if detail else ["Completed"]
                                if affected:
                                    parts.append(f"Affected: {', '.join(a.get('title','?')[:40] for a in affected[:5])}")
                                st.session_state["_proposal_approved"] = " | ".join(parts)
                            else: st.session_state["_proposal_approved"] = f"Approved: {p.get('title','')[:50]}"
                        else: st.session_state["_proposal_approved"] = f"⚠️ {result.get('error','Failed')}"
                    except requests.exceptions.ConnectionError: st.session_state["_proposal_approved"] = "❌ Orchestrator offline"
                    except Exception as e: st.session_state["_proposal_approved"] = f"❌ Error: {e}"
                st.rerun()
        with btn2:
            if st.button("🗑️ Dismiss", key=f"dismiss_{prop_id}", use_container_width=True):
                try: requests.post(f"{WATCHTOWER_CRON_URL}/proposals/{prop_id}/dismiss", timeout=5)
                except: pass
                st.session_state["_proposal_dismissed"] = p.get("title","")[:50]
                st.rerun()

# --- FRAGMENT SUPPORT ---
_FRAGMENT_AVAILABLE = hasattr(st, "fragment")

if _FRAGMENT_AVAILABLE:
    @st.fragment(run_every=3)
    def _chat_messages_fragment():
        active_id = get_active_conversation_id()
        history = conv_get_messages(limit=50, conv_id=active_id)
        task_is_running = False
        awaiting = st.session_state.get("_awaiting_task")
        if awaiting:
            task_id = awaiting["task_id"]
            try:
                r = requests.get(f"{GATEWAY_URL}/orch/queue/{task_id}", timeout=3)
                if r.status_code == 200:
                    task_state = r.json().get("state", "")
                    if task_state in ("completed", "failed", "cancelled"):
                        del st.session_state["_awaiting_task"]; st.rerun(scope="fragment")
                    elif task_state in ("queued", "running"):
                        task_is_running = True
                elif r.status_code == 404:
                    del st.session_state["_awaiting_task"]; st.rerun(scope="fragment")
            except: pass
            if time.time() - awaiting.get("queued_at", 0) > 300:
                st.session_state.pop("_awaiting_task", None); task_is_running = False
        chat_html = '<div class="chat-container"><div class="chat-messages" id="chat-scroll">'
        if not history:
            chat_html += '<div class="chat-message assistant"><div class="chat-bubble">👋 I\'m your Companion. Ask me anything or tell me what to do!</div></div>'
        for msg in history:
            role = msg.get("role", "user")
            content = html.escape(msg.get("content", "")).replace("\n", "<br>")
            ts = msg.get("timestamp", 0)
            time_str = datetime.fromtimestamp(ts).strftime("%H:%M") if ts else ""
            intent = msg.get("metadata", {}).get("intent", "")
            intent_badge = f'<div class="chat-intent">{intent}</div>' if intent and role == "assistant" else ""
            chat_html += f'<div class="chat-message {role}"><div class="chat-bubble">{content}</div><div class="chat-time">{time_str}</div>{intent_badge}</div>'
        if task_is_running:
            elapsed = time.time() - awaiting.get("queued_at", time.time())
            elapsed_str = f"{elapsed:.0f}s" if elapsed < 60 else f"{elapsed/60:.1f}m"
            chat_html += f'<div class="chat-message assistant"><div class="chat-bubble" style="opacity:0.7"><span class="chat-typing">Working on it ({elapsed_str})</span></div></div>'
        chat_html += '</div></div>'
        st.markdown(chat_html, unsafe_allow_html=True)
        import streamlit.components.v1 as _comp
        _comp.html('<script>var c=window.parent.document.getElementById("chat-scroll");if(c)c.scrollTop=c.scrollHeight;</script>', height=0)

    @st.fragment(run_every=5)
    def _status_fragment():
        activity = get_activity()
        is_active = activity.get("status") not in ["idle", "success", "failed"]
        dot_class = "heartbeat-dot active" if is_active else "heartbeat-dot"

        # Build status list — skip unconfigured optional services
        worker_port = CONFIG.PORT_WORKER
        try:
            gw_routes = requests.get(f"{GATEWAY_URL}/gateway/routes", timeout=2).json()
            worker_port = gw_routes.get("routes", {}).get("/worker", {}).get("port", worker_port)
        except: pass

        _search_label, _search_status = check_web_search()
        services_status = [
            ("Gateway",      check_service(CONFIG.PORT_GATEWAY)),
            ("Orchestrator", check_service(CONFIG.PORT_ORCHESTRATOR)),
            ("Coder",        check_service(CONFIG.PORT_CODER)),
            (f"Worker :{worker_port}", check_service(worker_port)),
            ("Watchtower",   check_service(CONFIG.PORT_WATCHTOWER)),
            (_search_label,  _search_status),
        ]

        status_cols = st.columns(len(services_status))
        for col, (name, online) in zip(status_cols, services_status):
            with col:
                if online is None:
                    st.markdown(f'<span class="status-pill" style="background:rgba(100,116,139,0.08);color:#4a5568;border:1px solid rgba(100,116,139,0.15)">⚪ {name}</span>', unsafe_allow_html=True)
                elif online:
                    st.markdown(f'<span class="status-pill status-online">🟢 {name}</span>', unsafe_allow_html=True)
                else:
                    st.markdown(f'<span class="status-pill status-offline">🔴 {name}</span>', unsafe_allow_html=True)

        st.markdown('<div class="section-divider"></div>', unsafe_allow_html=True)
        render_brain_activity()
        _render_roux_controls()
        _render_kill_switch()
        _render_budget_indicator()
        render_deploy_panel()

    @st.fragment(run_every=5)
    def _logs_fragment():
        st.markdown('<div class="section-divider"></div>', unsafe_allow_html=True)
        st.markdown('<div class="card-title">📺 Live Logs</div>', unsafe_allow_html=True)
        log_tabs = st.tabs(["🎯 Orch", "🧠 Coder", "⚙️ Worker", "👁️ Watch", "🌐 Gateway"])
        with log_tabs[0]: st.markdown(f'<div class="log-viewer">{html.escape(read_log("orchestrator"))}</div>', unsafe_allow_html=True)
        with log_tabs[1]: st.markdown(f'<div class="log-viewer">{html.escape(read_log("coder"))}</div>', unsafe_allow_html=True)
        with log_tabs[2]: st.markdown(f'<div class="log-viewer">{html.escape(read_log("worker"))}</div>', unsafe_allow_html=True)
        with log_tabs[3]: st.markdown(f'<div class="log-viewer">{html.escape(read_log("watchtower"))}</div>', unsafe_allow_html=True)
        with log_tabs[4]: st.markdown(f'<div class="log-viewer">{html.escape(read_log("gateway"))}</div>', unsafe_allow_html=True)
        if st.button("🧹 Clear Logs", use_container_width=True, key="clear_logs_frag"):
            for svc in ["orchestrator", "coder", "worker", "watchtower", "gateway"]:
                lf = LOGS_DIR / f"{svc}.log"
                if lf.exists(): lf.write_text("")
            st.success("Logs cleared!")


def main():
    activity = get_activity()
    is_active = activity.get("status") not in ["idle", "success", "failed"]
    dot_class = "heartbeat-dot active" if is_active else "heartbeat-dot"
    st.markdown(f'<h1 class="main-header">RouxYou <span class="{dot_class}"></span></h1>', unsafe_allow_html=True)
    st.markdown('<p class="sub-header">Sovereign AI Infrastructure</p>', unsafe_allow_html=True)

    if _FRAGMENT_AVAILABLE:
        _status_fragment()
    else:
        worker_port = CONFIG.PORT_WORKER
        try:
            gw_routes = requests.get(f"{GATEWAY_URL}/gateway/routes", timeout=2).json()
            worker_port = gw_routes.get("routes", {}).get("/worker", {}).get("port", worker_port)
        except: pass
        _search_label, _search_status = check_web_search()
        services_status = [
            ("Gateway",      check_service(CONFIG.PORT_GATEWAY)),
            ("Orchestrator", check_service(CONFIG.PORT_ORCHESTRATOR)),
            ("Coder",        check_service(CONFIG.PORT_CODER)),
            (f"Worker :{worker_port}", check_service(worker_port)),
            ("Watchtower",   check_service(CONFIG.PORT_WATCHTOWER)),
            (_search_label,  _search_status),
        ]
        status_cols = st.columns(len(services_status))
        for col, (name, online) in zip(status_cols, services_status):
            with col:
                if online is None: st.markdown(f'<span class="status-pill" style="background:rgba(100,116,139,0.08);color:#4a5568;border:1px solid rgba(100,116,139,0.15)">⚪ {name}</span>', unsafe_allow_html=True)
                elif online: st.markdown(f'<span class="status-pill status-online">🟢 {name}</span>', unsafe_allow_html=True)
                else: st.markdown(f'<span class="status-pill status-offline">🔴 {name}</span>', unsafe_allow_html=True)
        st.markdown('<div class="section-divider"></div>', unsafe_allow_html=True)
        render_brain_activity()
        _render_roux_controls()
        _render_kill_switch()
        _render_budget_indicator()
        render_deploy_panel()

    mode_tabs = st.tabs(["💬 Chat with Companion", "📋 Task Queue"])
    with mode_tabs[0]:
        render_chat_panel()
    with mode_tabs[1]:
        render_queue_panel()
        st.markdown("<hr>", unsafe_allow_html=True)
        st.markdown('<div class="card-title">🔔 System Proposals</div>', unsafe_allow_html=True)
        render_proposals_panel()

    if _FRAGMENT_AVAILABLE:
        _logs_fragment()
    else:
        st.markdown('<div class="section-divider"></div>', unsafe_allow_html=True)
        st.markdown('<div class="card-title">📺 Live Logs</div>', unsafe_allow_html=True)
        log_tabs = st.tabs(["🎯 Orch", "🧠 Coder", "⚙️ Worker", "👁️ Watch", "🌐 Gateway"])
        with log_tabs[0]: st.markdown(f'<div class="log-viewer">{html.escape(read_log("orchestrator"))}</div>', unsafe_allow_html=True)
        with log_tabs[1]: st.markdown(f'<div class="log-viewer">{html.escape(read_log("coder"))}</div>', unsafe_allow_html=True)
        with log_tabs[2]: st.markdown(f'<div class="log-viewer">{html.escape(read_log("worker"))}</div>', unsafe_allow_html=True)
        with log_tabs[3]: st.markdown(f'<div class="log-viewer">{html.escape(read_log("watchtower"))}</div>', unsafe_allow_html=True)
        with log_tabs[4]: st.markdown(f'<div class="log-viewer">{html.escape(read_log("gateway"))}</div>', unsafe_allow_html=True)
        if st.button("🧹 Clear Logs", use_container_width=True):
            for svc in ["orchestrator", "coder", "worker", "watchtower", "gateway"]:
                lf = LOGS_DIR / f"{svc}.log"; lf.write_text("") if lf.exists() else None
            st.success("Logs cleared!")
        from streamlit_autorefresh import st_autorefresh
        _awaiting_task = st.session_state.get("_awaiting_task")
        _refresh_ms = 5000 if (is_active or _awaiting_task) else 30000
        st_autorefresh(interval=_refresh_ms, limit=None, key="mc_refresh")


if __name__ == "__main__":
    main()
