"""
🎛️ ROUXYOU DASHBOARD
Phase 25.1: Proposal System Complete

Design: "Bayou" — matching rouxyou.com brand
- Swamp-black backgrounds (#080b09)
- Moss green + bayou blue accents
- Gold highlights (callouts, active states)
- Instrument Serif headers, DM Sans body, JetBrains Mono labels
- Film grain texture + atmospheric radial gradients

Run with: streamlit run dashboard.py
"""

import streamlit as st
import json
import time
import requests
from pathlib import Path
from datetime import datetime
import socket
import html

# --- CONFIGURATION ---
BASE_DIR = Path(__file__).parent
TASKS_FILE = BASE_DIR / "tasks.json"
LOGS_DIR = BASE_DIR / "logs"
ACTIVITY_FILE = BASE_DIR / "state" / "activity.json"
REFRESH_INTERVAL = 5

ROUX_URL = "http://localhost:8014"  # Roux Voice Service (Phase 29.4)
ORCHESTRATOR_URL = "http://localhost:8001"  # Phase 37: Event stream source

# Service ports
SERVICES = {
    "gateway": 8000,
    "orchestrator": 8001,
    "coder": 8002,
    "worker": 8003,
    "watchtower": 8010,
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
    /* Import fonts — matching rouxyou.com */
    @import url('https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=DM+Sans:wght@300;400;500;600;700&family=JetBrains+Mono:wght@300;400;500&display=swap');
    
    /* Root variables — RouxYou brand palette */
    :root {
        --bg-primary: #080b09;
        --bg-secondary: #0c110e;
        --bg-card: #121a15;
        --bg-input: #0a0f0b;
        --text-primary: #d4d0c8;
        --text-secondary: #9a9484;
        --text-muted: #6b5f50;
        --accent-pink: #c89b3c;
        --accent-purple: #3d6b48;
        --accent-gradient: linear-gradient(135deg, #c89b3c 0%, #3d6b48 100%);
        --success: #3d6b48;
        --warning: #c89b3c;
        --error: #8b3a3a;
        --border-subtle: rgba(42, 74, 50, 0.15);
        /* RouxYou extended palette */
        --moss: #2a4a32;
        --moss-light: #3d6b48;
        --bayou-blue: #1a2f3a;
        --water-blue: #2a4858;
        --mud: #3d2e1f;
        --mud-light: #6b5438;
        --cypress: #8b7a56;
        --gold: #c89b3c;
        --gold-dim: #a07d2e;
        --fog: #9a9484;
        --bone: #d4d0c8;
        --root: #5c7a4a;
    }
    
    /* Global styles */
    .stApp {
        background: var(--bg-primary);
        font-family: 'DM Sans', sans-serif;
        font-weight: 300;
        animation: smoothFadeIn 0.3s ease-out;
    }
    
    /* Swamp atmosphere — matching rouxyou.com */
    .stApp::before {
        content: '';
        position: fixed;
        inset: 0;
        background:
            radial-gradient(ellipse at 20% 50%, rgba(42, 74, 50, 0.06) 0%, transparent 60%),
            radial-gradient(ellipse at 80% 20%, rgba(26, 47, 58, 0.05) 0%, transparent 50%),
            radial-gradient(ellipse at 50% 80%, rgba(61, 46, 31, 0.04) 0%, transparent 40%);
        pointer-events: none;
        z-index: 0;
    }
    
    /* Film grain texture */
    .stApp::after {
        content: '';
        position: fixed;
        inset: 0;
        opacity: 0.025;
        background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E");
        pointer-events: none;
        z-index: 0;
    }
    
    @keyframes smoothFadeIn {
        from { opacity: 0.85; }
        to { opacity: 1; }
    }
    
    /* Hide Streamlit branding */
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    header {visibility: hidden;}
    
    /* Main container */
    .main .block-container {
        padding: 1rem 2rem;
        max-width: 100%;
    }
    
    /* Header styling — RouxYou wordmark */
    .main-header {
        font-family: 'Instrument Serif', serif;
        color: var(--gold);
        font-size: 2.5rem;
        font-weight: 400;
        margin-bottom: 0.3rem;
        letter-spacing: -0.02em;
    }
    
    /* Section dividers — moss-to-bayou gradient like rouxyou.com */
    .section-divider {
        height: 1px;
        background: linear-gradient(to right, var(--moss), var(--bayou-blue), transparent);
        margin: 1rem 0;
        opacity: 0.2;
    }
    
    /* Heartbeat indicator */
    .heartbeat-dot {
        display: inline-block;
        width: 10px;
        height: 10px;
        border-radius: 50%;
        background: var(--moss-light);
        margin-left: 0.5rem;
        vertical-align: middle;
        animation: heartbeat 2s ease-in-out infinite;
    }
    
    .heartbeat-dot.active {
        background: var(--gold);
        animation: heartbeat-active 1s ease-in-out infinite;
    }
    
    @keyframes heartbeat {
        0%, 100% { opacity: 0.4; transform: scale(1); }
        50% { opacity: 1; transform: scale(1.2); }
    }
    
    @keyframes heartbeat-active {
        0%, 100% { opacity: 0.6; transform: scale(1); box-shadow: 0 0 4px var(--gold); }
        50% { opacity: 1; transform: scale(1.4); box-shadow: 0 0 12px var(--gold); }
    }
    
    .sub-header {
        font-family: 'JetBrains Mono', monospace;
        color: var(--fog);
        font-size: 0.7rem;
        font-weight: 300;
        letter-spacing: 0.1em;
        text-transform: uppercase;
        margin-bottom: 1.5rem;
    }
    
    /* Bento Cards — matching rouxyou.com architecture cells */
    .bento-card {
        background: var(--bg-secondary);
        border: 1px solid rgba(42, 74, 50, 0.12);
        border-radius: 12px;
        padding: 1.25rem;
        margin-bottom: 1rem;
        transition: all 0.4s ease;
    }
    
    .bento-card:hover {
        background: rgba(42, 74, 50, 0.06);
        border-color: rgba(42, 74, 50, 0.25);
    }
    
    .bento-card.active {
        border-color: var(--gold-dim);
        box-shadow: 0 0 30px rgba(200, 155, 60, 0.1);
    }
    
    .card-title {
        font-family: 'JetBrains Mono', monospace;
        color: var(--fog);
        font-size: 0.7rem;
        font-weight: 400;
        text-transform: uppercase;
        letter-spacing: 0.12em;
        margin-bottom: 1rem;
        display: flex;
        align-items: center;
        gap: 0.5rem;
    }
    
    /* Command Center */
    .command-center {
        background: linear-gradient(135deg, rgba(42, 74, 50, 0.1) 0%, rgba(200, 155, 60, 0.05) 100%);
        border: 2px solid var(--moss);
        border-radius: 16px;
        padding: 1.5rem;
        margin-bottom: 1.5rem;
        box-shadow: 0 0 40px rgba(42, 74, 50, 0.1);
    }
    
    /* Status pills — JetBrains Mono like port numbers on rouxyou.com */
    .status-pill {
        display: inline-flex;
        align-items: center;
        gap: 0.35rem;
        padding: 0.3rem 0.7rem;
        border-radius: 9999px;
        font-family: 'JetBrains Mono', monospace;
        font-size: 0.65rem;
        font-weight: 400;
        letter-spacing: 0.03em;
    }
    
    .status-online {
        background: rgba(61, 107, 72, 0.15);
        color: var(--moss-light);
        border: 1px solid rgba(61, 107, 72, 0.3);
    }
    
    .status-offline {
        background: rgba(139, 58, 58, 0.15);
        color: #8b3a3a;
        border: 1px solid rgba(139, 58, 58, 0.3);
    }
    
    /* Log viewer */
    .log-viewer {
        background: var(--bg-input);
        border: 1px solid var(--border-subtle);
        border-radius: 8px;
        padding: 1rem;
        font-family: 'JetBrains Mono', monospace;
        font-size: 0.8rem;
        line-height: 1.6;
        color: var(--text-secondary);
        height: 350px;
        overflow-y: auto;
        white-space: pre-wrap;
        word-wrap: break-word;
    }
    
    .log-viewer::-webkit-scrollbar {
        width: 6px;
    }
    
    .log-viewer::-webkit-scrollbar-track {
        background: var(--bg-input);
    }
    
    .log-viewer::-webkit-scrollbar-thumb {
        background: var(--text-muted);
        border-radius: 3px;
    }
    
    /* Task cards */
    .task-card {
        background: var(--bg-input);
        border: 1px solid var(--border-subtle);
        border-radius: 12px;
        padding: 1rem;
        margin-bottom: 0.75rem;
        border-left: 3px solid;
        transition: all 0.2s ease;
    }
    
    .task-card:hover {
        background: rgba(15, 23, 42, 0.8);
    }
    
    .task-pending { border-left-color: var(--warning); }
    .task-blocked { border-left-color: var(--accent-pink); }
    .task-active { border-left-color: var(--gold); }
    .task-completed { border-left-color: var(--success); }
    .task-failed { border-left-color: var(--error); }
    
    .task-title {
        color: var(--text-primary);
        font-weight: 500;
        font-size: 0.9rem;
        margin-bottom: 0.25rem;
    }
    
    .task-meta {
        color: var(--text-muted);
        font-size: 0.75rem;
    }
    
    /* Metrics */
    .metric-value {
        font-size: 2rem;
        font-weight: 700;
        background: var(--accent-gradient);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        background-clip: text;
    }
    
    .metric-label {
        color: var(--text-muted);
        font-size: 0.75rem;
        text-transform: uppercase;
        letter-spacing: 0.05em;
    }
    
    /* Blocked task highlight */
    .blocked-alert {
        background: linear-gradient(135deg, rgba(200, 155, 60, 0.12) 0%, rgba(139, 58, 58, 0.12) 100%);
        border: 2px solid var(--gold-dim);
        border-radius: 12px;
        padding: 1rem;
        margin: 0.75rem 0;
    }
    
    /* Buttons */
    .stButton > button {
        background: var(--accent-gradient);
        color: white;
        border: none;
        border-radius: 8px;
        padding: 0.5rem 1rem;
        font-weight: 500;
        transition: all 0.2s ease;
    }
    
    .stButton > button:hover {
        transform: translateY(-1px);
        box-shadow: 0 4px 20px rgba(200, 155, 60, 0.25);
    }
    
    /* Input fields */
    .stTextInput > div > div > input {
        background: var(--bg-input);
        border: 1px solid var(--border-subtle);
        border-radius: 8px;
        color: var(--text-primary);
        font-family: 'DM Sans', sans-serif;
    }
    
    .stTextInput > div > div > input:focus {
        border-color: var(--moss-light);
        box-shadow: 0 0 0 2px rgba(42, 74, 50, 0.3);
    }
    
    /* Tabs */
    .stTabs [data-baseweb="tab-list"] {
        gap: 0.5rem;
        background: transparent;
    }
    
    .stTabs [data-baseweb="tab"] {
        background: var(--bg-input);
        border-radius: 8px;
        color: var(--text-secondary);
        padding: 0.5rem 1rem;
        font-size: 0.8rem;
    }
    
    .stTabs [aria-selected="true"] {
        background: var(--accent-gradient);
        color: white;
    }
    
    /* Expander */
    .streamlit-expanderHeader {
        background: var(--bg-input);
        border-radius: 8px;
        color: var(--text-primary);
    }
    
    /* Select box */
    .stSelectbox > div > div {
        background: var(--bg-input);
        border-color: var(--border-subtle);
    }
    
    /* Checkbox */
    .stCheckbox label {
        color: var(--text-secondary);
    }
    
    /* Divider */
    hr {
        border-color: var(--border-subtle);
        margin: 1.5rem 0;
    }
    
    /* Brain Activity Panel — callout style from rouxyou.com */
    .brain-activity {
        background: rgba(200, 155, 60, 0.03);
        border-left: 2px solid var(--gold-dim);
        border-radius: 0 8px 8px 0;
        padding: 1rem 1.25rem;
        margin-bottom: 1rem;
        transition: all 0.3s ease;
    }
    
    .brain-activity.active {
        background: rgba(200, 155, 60, 0.06);
        border-left: 3px solid var(--gold);
        box-shadow: 0 0 20px rgba(200, 155, 60, 0.08);
        animation: pulse-glow 2s ease-in-out infinite;
    }
    
    @keyframes pulse-glow {
        0%, 100% { box-shadow: 0 0 20px rgba(200, 155, 60, 0.12); }
        50% { box-shadow: 0 0 30px rgba(200, 155, 60, 0.2); }
    }
    
    .brain-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        margin-bottom: 0.75rem;
    }
    
    .brain-title {
        font-family: 'JetBrains Mono', monospace;
        color: var(--fog);
        font-size: 0.7rem;
        font-weight: 400;
        text-transform: uppercase;
        letter-spacing: 0.1em;
        display: flex;
        align-items: center;
        gap: 0.5rem;
    }
    
    .brain-status {
        font-size: 0.7rem;
        padding: 0.25rem 0.6rem;
        border-radius: 9999px;
        font-weight: 500;
    }
    
    .brain-status.idle {
        background: rgba(100, 116, 139, 0.2);
        color: var(--text-muted);
    }
    
    .brain-status.active {
        background: rgba(200, 155, 60, 0.2);
        color: var(--gold);
    }
    
    .brain-status.success {
        background: rgba(16, 185, 129, 0.2);
        color: var(--success);
    }
    
    .brain-status.failed {
        background: rgba(239, 68, 68, 0.2);
        color: var(--error);
    }
    
    .brain-thought {
        color: var(--text-secondary);
        font-size: 0.9rem;
        font-style: italic;
        margin-bottom: 0.75rem;
        padding-left: 1.5rem;
        border-left: 2px solid var(--gold-dim);
    }
    
    .brain-progress {
        display: flex;
        align-items: center;
        gap: 0.75rem;
    }
    
    .brain-progress-bar {
        flex: 1;
        height: 6px;
        background: rgba(100, 116, 139, 0.2);
        border-radius: 3px;
        overflow: hidden;
    }
    
    .brain-progress-fill {
        height: 100%;
        background: var(--accent-gradient);
        border-radius: 3px;
        transition: width 0.3s ease;
    }
    
    .brain-progress-text {
        color: var(--text-muted);
        font-size: 0.75rem;
        min-width: 60px;
        text-align: right;
    }
    
    .brain-task {
        color: var(--text-primary);
        font-size: 0.85rem;
        font-weight: 500;
        margin-bottom: 0.5rem;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }
    
    .brain-agent {
        color: var(--text-muted);
        font-size: 0.7rem;
        text-transform: uppercase;
        letter-spacing: 0.03em;
    }
    
    /* Chat Panel */
    .chat-container {
        background: var(--bg-card);
        border: 1px solid var(--border-subtle);
        border-radius: 12px;
        padding: 1rem;
        height: 500px;
        display: flex;
        flex-direction: column;
    }
    
    .chat-messages {
        flex: 1;
        min-height: 0;
        overflow-y: auto;
        padding-right: 0.5rem;
        margin-bottom: 1rem;
    }
    
    .chat-message {
        margin-bottom: 1rem;
        animation: fadeIn 0.3s ease;
    }
    
    @keyframes fadeIn {
        from { opacity: 0; transform: translateY(10px); }
        to { opacity: 1; transform: translateY(0); }
    }
    
    .chat-message.user {
        text-align: right;
    }
    
    .chat-message.assistant {
        text-align: left;
    }
    
    .chat-bubble {
        display: inline-block;
        max-width: 80%;
        padding: 0.75rem 1rem;
        border-radius: 16px;
        font-size: 0.9rem;
        line-height: 1.5;
    }
    
    .chat-message.user .chat-bubble {
        background: linear-gradient(135deg, var(--moss) 0%, var(--bayou-blue) 100%);
        color: var(--bone);
        border-bottom-right-radius: 4px;
    }
    
    .chat-message.assistant .chat-bubble {
        background: var(--bg-input);
        color: var(--text-primary);
        border: 1px solid var(--border-subtle);
        border-bottom-left-radius: 4px;
    }
    
    .chat-time {
        font-size: 0.65rem;
        color: var(--text-muted);
        margin-top: 0.25rem;
    }
    
    .chat-intent {
        font-size: 0.6rem;
        color: var(--gold-dim);
        margin-top: 0.15rem;
    }
    
    .chat-typing {
        color: var(--text-muted);
        font-style: italic;
        font-size: 0.85rem;
    }
    
    .chat-typing::after {
        content: '...';
        animation: dots 1.5s infinite;
    }
    
    @keyframes dots {
        0%, 20% { content: '.'; }
        40% { content: '..'; }
        60%, 100% { content: '...'; }
    }
    
    /* Conversation Bar */
    .stSelectbox > div > div {
        font-size: 0.85rem;
    }
    
    /* Proposal Cards (Phase 25) */
    .proposal-card {
        background: var(--bg-input);
        border: 1px solid var(--border-subtle);
        border-radius: 12px;
        padding: 1rem 1.25rem;
        margin-bottom: 0.75rem;
        border-left: 4px solid;
        transition: all 0.2s ease;
    }
    .proposal-card:hover {
        background: rgba(18, 26, 21, 0.9);
        box-shadow: 0 0 20px rgba(42, 74, 50, 0.08);
    }
    .proposal-card.cat-health { border-left-color: #8b3a3a; }
    .proposal-card.cat-memory { border-left-color: var(--moss-light); }
    .proposal-card.cat-codebase { border-left-color: var(--water-blue); }
    .proposal-card.cat-tasks { border-left-color: var(--gold); }
    .proposal-card.cat-resources { border-left-color: var(--mud-light); }
    .proposal-card.cat-skills { border-left-color: var(--root); }
    .proposal-card.cat-optimization { border-left-color: var(--cypress); }
    .proposal-title {
        color: var(--text-primary);
        font-weight: 600;
        font-size: 0.9rem;
        margin-bottom: 0.35rem;
    }
    .proposal-desc {
        color: var(--text-secondary);
        font-size: 0.8rem;
        margin-bottom: 0.5rem;
        line-height: 1.4;
    }
    .proposal-meta {
        display: flex;
        align-items: center;
        gap: 0.75rem;
        flex-wrap: wrap;
    }
    .proposal-badge {
        display: inline-flex;
        align-items: center;
        padding: 0.2rem 0.5rem;
        border-radius: 6px;
        font-size: 0.7rem;
        font-weight: 500;
    }
    .proposal-badge.priority {
        background: rgba(200, 155, 60, 0.15);
        color: var(--gold);
        border: 1px solid rgba(200, 155, 60, 0.3);
    }
    .proposal-badge.category {
        background: rgba(42, 74, 50, 0.15);
        color: var(--moss-light);
        border: 1px solid rgba(42, 74, 50, 0.3);
    }
    .proposal-badge.reversible {
        background: rgba(92, 122, 74, 0.15);
        color: var(--root);
        border: 1px solid rgba(92, 122, 74, 0.3);
    }
    .proposal-badge.irreversible {
        background: rgba(139, 58, 58, 0.15);
        color: #8b3a3a;
        border: 1px solid rgba(139, 58, 58, 0.3);
    }
    .proposal-action {
        color: var(--text-muted);
        font-size: 0.75rem;
        font-family: 'JetBrains Mono', monospace;
        margin-top: 0.4rem;
        padding: 0.3rem 0.5rem;
        background: rgba(100, 116, 139, 0.1);
        border-radius: 4px;
    }
    .proposal-evidence {
        color: var(--text-muted);
        font-size: 0.7rem;
        font-style: italic;
        margin-top: 0.25rem;
    }
    .proposals-empty {
        text-align: center;
        padding: 3rem 1rem;
        color: var(--text-muted);
    }
    .proposals-empty-icon { font-size: 3rem; margin-bottom: 0.75rem; }
    .proposals-empty-text { font-size: 0.9rem; color: var(--text-secondary); }
    .proposals-empty-sub { font-size: 0.75rem; color: var(--text-muted); margin-top: 0.25rem; }

    /* Phase 37b: Event Feed */
    .event-feed {
        background: var(--bg-input);
        border: 1px solid var(--border-subtle);
        border-radius: 8px;
        padding: 0.75rem;
        max-height: 280px;
        overflow-y: auto;
        margin-bottom: 1rem;
    }

    .event-feed::-webkit-scrollbar { width: 4px; }
    .event-feed::-webkit-scrollbar-track { background: var(--bg-input); }
    .event-feed::-webkit-scrollbar-thumb { background: var(--text-muted); border-radius: 2px; }

    .event-item {
        display: flex;
        align-items: flex-start;
        gap: 0.5rem;
        padding: 0.35rem 0;
        border-bottom: 1px solid rgba(42, 74, 50, 0.08);
        animation: fadeIn 0.3s ease;
    }

    .event-item:last-child { border-bottom: none; }

    .event-icon {
        font-size: 0.85rem;
        min-width: 1.2rem;
        text-align: center;
        padding-top: 0.1rem;
    }

    .event-body {
        flex: 1;
        min-width: 0;
    }

    .event-text {
        color: var(--text-secondary);
        font-size: 0.78rem;
        line-height: 1.4;
        word-break: break-word;
    }

    .event-meta {
        display: flex;
        align-items: center;
        gap: 0.5rem;
        margin-top: 0.15rem;
    }

    .event-agent {
        font-family: 'JetBrains Mono', monospace;
        font-size: 0.6rem;
        color: var(--text-muted);
        text-transform: uppercase;
        letter-spacing: 0.05em;
        padding: 0.1rem 0.35rem;
        background: rgba(42, 74, 50, 0.12);
        border-radius: 4px;
    }

    .event-time {
        font-family: 'JetBrains Mono', monospace;
        font-size: 0.6rem;
        color: var(--text-muted);
    }

    .event-type-badge {
        font-family: 'JetBrains Mono', monospace;
        font-size: 0.55rem;
        padding: 0.08rem 0.3rem;
        border-radius: 3px;
        text-transform: uppercase;
        letter-spacing: 0.04em;
    }

    /* Phase 35b: Cost Tracker Bar */
    .cost-bar {
        display: flex;
        align-items: center;
        gap: 1rem;
        padding: 0.5rem 0.75rem;
        background: rgba(26, 47, 58, 0.15);
        border: 1px solid rgba(42, 72, 88, 0.2);
        border-radius: 8px;
        margin-bottom: 0.75rem;
    }

    .cost-metric {
        text-align: center;
        min-width: 70px;
    }

    .cost-value {
        font-family: 'JetBrains Mono', monospace;
        font-size: 0.85rem;
        font-weight: 600;
        color: var(--bone);
    }

    .cost-label {
        font-family: 'JetBrains Mono', monospace;
        font-size: 0.55rem;
        color: var(--text-muted);
        text-transform: uppercase;
        letter-spacing: 0.06em;
    }

    .cost-divider {
        width: 1px;
        height: 28px;
        background: rgba(42, 74, 50, 0.2);
    }

    /* Phase 25.2: Auto-approve badges */
    .auto-badge {
        display: inline-flex;
        align-items: center;
        gap: 0.25rem;
        padding: 0.15rem 0.45rem;
        border-radius: 6px;
        font-size: 0.6rem;
        font-weight: 500;
        font-family: 'JetBrains Mono', monospace;
    }
    .auto-badge.auto-approved {
        background: rgba(139, 92, 246, 0.15);
        color: #8b5cf6;
        border: 1px solid rgba(139, 92, 246, 0.3);
    }
    .auto-badge.human-approved {
        background: rgba(16, 185, 129, 0.15);
        color: #10b981;
        border: 1px solid rgba(16, 185, 129, 0.3);
    }
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

def add_task(title, description, priority=5, task_type="user_goal"):
    tasks = load_tasks()
    new_task = {
        "id": f"{hash(title + str(time.time())) % 100000000:08x}",
        "title": title,
        "description": description,
        "type": task_type,
        "priority": priority,
        "status": "pending",
        "created_at": time.time(),
        "context": {}
    }
    tasks.append(new_task)
    save_tasks(tasks)
    return new_task["id"]

def approve_task(task_id):
    tasks = load_tasks()
    for task in tasks:
        if task["id"] == task_id and task["status"] == "pending":
            task["status"] = "approved"
            save_tasks(tasks)
            return True
    return False

def reject_task(task_id):
    tasks = load_tasks()
    tasks = [t for t in tasks if t["id"] != task_id]
    save_tasks(tasks)

def unblock_task(task_id, user_response):
    tasks = load_tasks()
    for task in tasks:
        if task["id"] == task_id and task["status"] == "blocked":
            task["user_response"] = user_response
            task["status"] = "approved"
            task["retry_count"] = task.get("retry_count", 0) + 1
            save_tasks(tasks)
            return True
    return False

def check_service(port):
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        result = sock.connect_ex(("localhost", port))
        sock.close()
        return result == 0
    except:
        return False

# --- CACHED HEALTH CHECKS (prevent flickering on refresh) ---
_health_cache = {}  # {service_name: (timestamp, result)}
HEALTH_CACHE_TTL = 15  # seconds — recheck every 15s, not every refresh

def _cached_check(name, check_fn):
    """Cache health check results to prevent flickering on page refresh."""
    now = time.time()
    if name in _health_cache:
        ts, result = _health_cache[name]
        if now - ts < HEALTH_CACHE_TTL:
            return result
    result = check_fn()
    _health_cache[name] = (now, result)
    return result

def check_searxng():
    def _check():
        try:
            r = requests.get("http://localhost:8888/search", params={"q": "test", "format": "json"}, timeout=3)
            return r.status_code == 200
        except:
            return False
    return _cached_check("searxng", _check)

def check_proxmox():
    def _check():
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2)
            result = sock.connect_ex(("localhost", 8006))
            sock.close()
            return result == 0
        except:
            return False
    return _cached_check("proxmox", _check)

def read_log(service_name, lines=100):
    log_file = LOGS_DIR / f"{service_name}.log"
    if not log_file.exists():
        return f"📭 Waiting for {service_name} logs..."
    try:
        with open(log_file, 'r', encoding='utf-8', errors='replace') as f:
            all_lines = f.readlines()
            if not all_lines:
                return f"📭 Log empty for {service_name}"
            return ''.join(all_lines[-lines:])
    except Exception as e:
        return f"❌ Error: {e}"

def get_status_emoji(status):
    return {"pending": "⏳", "approved": "🚀", "in_progress": "🔄", "blocked": "🚧", "completed": "✅", "failed": "❌"}.get(status, "❓")

def get_activity():
    """Read current brain activity state.
    Phase 37b: Tries REST event endpoint first, falls back to activity.json file.
    Also fetches recent events for the Event Feed widget.
    """
    # Try REST endpoint (faster, no disk I/O, gets typed events)
    try:
        resp = requests.get(f"{ORCHESTRATOR_URL}/events/recent", params={"n": 20}, timeout=1.5)
        if resp.status_code == 200:
            data = resp.json()
            events = data.get("events", [])
            st.session_state["_recent_events"] = events

            # Derive brain activity from the most recent non-heartbeat event
            for evt in reversed(events):
                etype = evt.get("type", "")
                if etype == "heartbeat":
                    continue
                edata = evt.get("data", {})
                agent = evt.get("agent", "")
                task_id = evt.get("task_id", "")

                # Map event types to activity status
                if etype == "thought":
                    return {
                        "task_id": task_id, "task_title": edata.get("message", "")[:80],
                        "agent": agent, "status": "active", "step": 0, "total_steps": 0,
                        "thought": edata.get("message", ""), "plan": []
                    }
                elif etype == "task_started":
                    return {
                        "task_id": task_id, "task_title": edata.get("title", ""),
                        "agent": agent, "status": "active", "step": 0, "total_steps": 0,
                        "thought": f"Starting: {edata.get('title', '')}", "plan": []
                    }
                elif etype in ("step_started", "step_completed"):
                    return {
                        "task_id": task_id, "task_title": edata.get("description", ""),
                        "agent": agent, "status": "active",
                        "step": edata.get("step", 0), "total_steps": edata.get("total", 0),
                        "thought": edata.get("description", ""), "plan": []
                    }
                elif etype == "task_completed":
                    return {
                        "task_id": task_id, "task_title": edata.get("summary", "Complete"),
                        "agent": agent, "status": "success", "step": 0, "total_steps": 0,
                        "thought": edata.get("summary", "Task completed."), "plan": []
                    }
                elif etype == "task_failed":
                    return {
                        "task_id": task_id, "task_title": edata.get("summary", "Failed"),
                        "agent": agent, "status": "failed", "step": 0, "total_steps": 0,
                        "thought": edata.get("summary", "Task failed."), "plan": []
                    }
                elif etype == "error":
                    return {
                        "task_id": task_id, "task_title": "Error",
                        "agent": agent, "status": "failed", "step": 0, "total_steps": 0,
                        "thought": edata.get("message", "Unknown error"), "plan": []
                    }
                # For tool_called, llm_call, etc. — keep looking for a more meaningful event
                continue
    except Exception:
        pass  # Orchestrator may not be running — fall back to file

    # File-based fallback (always works, backward compatible)
    if ACTIVITY_FILE.exists():
        try:
            with open(ACTIVITY_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "task_id": None,
        "task_title": None,
        "agent": None,
        "status": "idle",
        "step": 0,
        "total_steps": 0,
        "thought": "System idle. Waiting for tasks...",
        "plan": []
    }

def render_brain_activity():
    """Render the Brain Activity panel"""
    activity = get_activity()
    status = activity.get("status", "idle")
    thought = activity.get("thought", "System idle. Waiting for tasks...")
    task_title = activity.get("task_title", "")
    agent = activity.get("agent", "")
    step = activity.get("step", 0)
    total_steps = activity.get("total_steps", 0)
    
    # Calculate progress
    progress_pct = (step / total_steps * 100) if total_steps > 0 else 0
    
    # Status badge styling
    status_class = status if status in ["idle", "active", "success", "failed"] else "active"
    status_text = {"idle": "IDLE", "active": "THINKING", "success": "COMPLETE", "failed": "FAILED"}.get(status, "WORKING")
    
    # Escape HTML in thought text
    thought_safe = html.escape(thought)
    task_safe = html.escape(task_title) if task_title else ""
    
    # Panel class (add 'active' for glow effect when working)
    panel_class = "brain-activity active" if status not in ["idle", "success"] else "brain-activity"
    
    # Build progress bar HTML if needed
    progress_html = ""
    if total_steps > 0:
        progress_html = f'''<div class="brain-progress"><div class="brain-progress-bar"><div class="brain-progress-fill" style="width: {progress_pct}%"></div></div><span class="brain-progress-text">Step {step}/{total_steps}</span></div>'''
    
    # Build task line if present
    task_html = f'<div class="brain-task">📋 {task_safe}</div>' if task_title else ""
    
    # Build agent line if present  
    agent_html = f'<div class="brain-agent">Agent: {agent}</div>' if agent else ""
    
    # Construct full HTML in one line (Streamlit can be picky about whitespace)
    html_content = f'<div class="{panel_class}"><div class="brain-header"><span class="brain-title">🧠 Brain Activity</span><span class="brain-status {status_class}">{status_text}</span></div>{task_html}{agent_html}<div class="brain-thought">💭 "{thought_safe}"</div>{progress_html}</div>'
    
    st.markdown(html_content, unsafe_allow_html=True)

# --- EVENT FEED (Phase 37b) ---
_EVENT_ICONS = {
    "task_started": "🚀",
    "task_completed": "✅",
    "task_failed": "❌",
    "thought": "💭",
    "plan_created": "📋",
    "step_started": "▶️",
    "step_completed": "✔️",
    "status_changed": "🔄",
    "tool_called": "🔧",
    "tool_result": "📦",
    "service_health": "💚",
    "proposal_created": "🔔",
    "proposal_approved": "👍",
    "proposal_executed": "⚡",
    "error": "🔴",
    "llm_call": "🧠",
    "heartbeat": "💓",
}

_EVENT_TYPE_COLORS = {
    "task_started": "#3d6b48",
    "task_completed": "#3d6b48",
    "task_failed": "#8b3a3a",
    "thought": "#c89b3c",
    "plan_created": "#2a4858",
    "step_started": "#6b5438",
    "step_completed": "#3d6b48",
    "tool_called": "#5c7a4a",
    "tool_result": "#5c7a4a",
    "error": "#8b3a3a",
    "llm_call": "#8b7a56",
}

def _event_text(event: dict) -> str:
    """Extract a human-readable summary from a typed event."""
    etype = event.get("type", "")
    data = event.get("data", {})

    if etype == "thought":
        return data.get("message", "")[:120]
    elif etype == "task_started":
        return f"Task started: {data.get('title', '')[:80]}"
    elif etype == "task_completed":
        return f"Task complete: {data.get('summary', '')[:80]}" if data.get("summary") else "Task completed"
    elif etype == "task_failed":
        return f"Task failed: {data.get('summary', '')[:80]}" if data.get("summary") else "Task failed"
    elif etype == "plan_created":
        steps = data.get("steps", [])
        return f"Plan: {len(steps)} steps — {steps[0][:60] if steps else ''}"
    elif etype in ("step_started", "step_completed"):
        step = data.get("step", "?")
        total = data.get("total", "?")
        desc = data.get("description", "")[:60]
        verb = "Started" if etype == "step_started" else "Done"
        return f"{verb} step {step}/{total}: {desc}"
    elif etype == "tool_called":
        return f"Calling {data.get('tool', '?')} → {data.get('target', '')[:60]}"
    elif etype == "tool_result":
        ok = "ok" if data.get("success") else "FAIL"
        return f"{data.get('tool', '?')}: {ok} — {data.get('summary', '')[:60]}"
    elif etype == "error":
        return f"Error: {data.get('message', '')[:100]}"
    elif etype == "llm_call":
        model = data.get("model", "?")
        inp = data.get("input_tokens", 0)
        out = data.get("output_tokens", 0)
        cost = data.get("cost_usd", 0)
        cost_str = f" (${cost:.4f})" if cost > 0 else ""
        return f"{model}: {inp}in/{out}out{cost_str}"
    elif etype == "status_changed":
        return f"Status → {data.get('status', '?')}"
    elif etype == "service_health":
        svc = data.get("service", "?")
        ok = "healthy" if data.get("healthy") else "DOWN"
        return f"{svc}: {ok}"
    elif etype == "heartbeat":
        return ""
    else:
        return str(data)[:80] if data else etype


def render_event_feed(events: list):
    """Render a scrollable event feed from typed StreamEvents."""
    # Filter out heartbeats and empty
    visible = [e for e in events if e.get("type") != "heartbeat"]
    if not visible:
        st.markdown(
            '<div class="event-feed" style="text-align:center;padding:2rem;color:var(--text-muted)">'
            '📡 Waiting for events...</div>',
            unsafe_allow_html=True
        )
        return

    feed_html = '<div class="event-feed">'
    for event in reversed(visible):  # newest first
        etype = event.get("type", "")
        agent = event.get("agent", "system")
        ts = event.get("timestamp", 0)
        icon = _EVENT_ICONS.get(etype, "📌")
        text = html.escape(_event_text(event))
        color = _EVENT_TYPE_COLORS.get(etype, "#6b5f50")

        if not text:
            continue

        try:
            time_str = datetime.fromtimestamp(ts).strftime("%H:%M:%S")
        except Exception:
            time_str = ""

        feed_html += (
            f'<div class="event-item">'
            f'<span class="event-icon">{icon}</span>'
            f'<div class="event-body">'
            f'<div class="event-text">{text}</div>'
            f'<div class="event-meta">'
            f'<span class="event-agent">{agent}</span>'
            f'<span class="event-type-badge" style="background:{color}22;color:{color};border:1px solid {color}44">{etype.replace("_"," ")}</span>'
            f'<span class="event-time">{time_str}</span>'
            f'</div></div></div>'
        )
    feed_html += '</div>'
    st.markdown(feed_html, unsafe_allow_html=True)


# --- COST TRACKER DISPLAY (Phase 35b) ---
def _format_tokens(n: int) -> str:
    """Format token count for display."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def render_cost_bar():
    """Render cost/token tracking bar from orchestrator."""
    try:
        resp = requests.get(f"{ORCHESTRATOR_URL}/cost/status", timeout=1.5)
        if resp.status_code != 200:
            return
        cost_data = resp.json()
    except Exception:
        return  # Orchestrator offline or no cost data

    session = cost_data.get("session", {})
    daily = cost_data.get("daily", {})

    s_calls = session.get("calls", 0)
    if s_calls == 0:
        return  # No LLM calls yet this session — don't clutter UI

    s_tokens = session.get("total_tokens", 0)
    s_cost = session.get("cost_display", "free")
    d_tokens = daily.get("total_tokens", 0)
    d_cost = daily.get("cost_display", "free")
    uptime = cost_data.get("session_uptime_minutes", 0)

    st.markdown(
        f'<div class="cost-bar">'
        f'<div class="cost-metric"><div class="cost-value">{s_calls}</div><div class="cost-label">LLM Calls</div></div>'
        f'<div class="cost-divider"></div>'
        f'<div class="cost-metric"><div class="cost-value">{_format_tokens(s_tokens)}</div><div class="cost-label">Session Tokens</div></div>'
        f'<div class="cost-divider"></div>'
        f'<div class="cost-metric"><div class="cost-value">{s_cost}</div><div class="cost-label">Session Cost</div></div>'
        f'<div class="cost-divider"></div>'
        f'<div class="cost-metric"><div class="cost-value">{_format_tokens(d_tokens)}</div><div class="cost-label">Daily Tokens</div></div>'
        f'<div class="cost-divider"></div>'
        f'<div class="cost-metric"><div class="cost-value">{d_cost}</div><div class="cost-label">Daily Cost</div></div>'
        f'<div class="cost-divider"></div>'
        f'<div class="cost-metric"><div class="cost-value">{uptime}m</div><div class="cost-label">Uptime</div></div>'
        f'</div>',
        unsafe_allow_html=True
    )


# --- LLM PROVIDER STATUS (Phase 34b) ---
def render_llm_status():
    """Show registered LLM providers, aliases, and health from /llm/status."""
    try:
        resp = requests.get(f"{ORCHESTRATOR_URL}/llm/status", timeout=2)
        if resp.status_code != 200:
            return
        data = resp.json()
    except Exception:
        return  # Orchestrator offline

    if not data.get("initialized"):
        return

    providers = data.get("providers", [])
    health = data.get("health", {})
    aliases = data.get("aliases", {})

    if not providers:
        return

    # Provider pills
    pills_html = '<div style="display:flex;align-items:center;gap:0.5rem;flex-wrap:wrap;margin-bottom:0.5rem">'
    pills_html += '<span style="font-family:JetBrains Mono,monospace;font-size:0.65rem;color:var(--fog);text-transform:uppercase;letter-spacing:0.08em;min-width:70px">LLM:</span>'
    for p in providers:
        ok = health.get(p, False)
        if ok:
            pills_html += f'<span class="status-pill status-online" style="font-size:0.6rem">🟢 {p}</span>'
        else:
            pills_html += f'<span class="status-pill status-offline" style="font-size:0.6rem">🔴 {p}</span>'

    # Compact alias display — just show the key ones
    key_aliases = ["router", "coder", "companion", "bigbrain"]
    alias_parts = []
    for a in key_aliases:
        if a in aliases:
            alias_parts.append(f'{a}→{aliases[a].split("/")[-1][:20]}')
    if alias_parts:
        alias_str = " · ".join(alias_parts)
        pills_html += f'<span style="font-family:JetBrains Mono,monospace;font-size:0.55rem;color:var(--text-muted);margin-left:0.5rem">{alias_str}</span>'

    pills_html += '</div>'
    st.markdown(pills_html, unsafe_allow_html=True)


# --- ROUX VOICE CONTROLS (Phase 29.4b) ---
def _render_roux_controls():
    """Render Roux VAD wake/sleep controls inline with the status bar."""
    try:
        r = requests.get(f"{ROUX_URL}/voice/vad/status", timeout=1.5)
        if r.status_code != 200:
            return
        vad = r.json()
    except:
        return  # Roux offline — render nothing, don't clutter the UI

    vad_active  = vad.get("vad_active", False)
    vad_awake   = vad.get("vad_awake", False)
    is_speaking = vad.get("is_speaking", False)

    if not vad_active:
        # VAD loop isn't running — show a muted pill + start button
        rc1, rc2 = st.columns([6, 1])
        with rc1:
            st.markdown(
                '<span class="status-pill status-offline">🔇 Roux — VAD off</span>',
                unsafe_allow_html=True
            )
        with rc2:
            if st.button("▶ Start VAD", key="roux_vad_start", help="Start always-listening loop"):
                try:
                    requests.post(f"{ROUX_URL}/voice/vad/toggle", timeout=3)
                except:
                    pass
                st.rerun()
        return

    # VAD is running — show state + wake/sleep controls
    if is_speaking:
        state_label = "🔊 Speaking"
        state_style = "background:rgba(200,155,60,0.2);color:#c89b3c;border:1px solid rgba(200,155,60,0.4)"
    elif vad_awake:
        state_label = "👂 Listening"
        state_style = "background:rgba(61,107,72,0.2);color:#3d6b48;border:1px solid rgba(61,107,72,0.4)"
    else:
        state_label = "😴 Sleeping"
        state_style = "background:rgba(100,116,139,0.15);color:#6b7280;border:1px solid rgba(100,116,139,0.25)"

    rc1, rc2, rc3, rc4 = st.columns([3, 1, 1, 1])
    with rc1:
        st.markdown(
            f'<span class="status-pill" style="{state_style}">🎙️ Roux &nbsp;·&nbsp; {state_label}</span>',
            unsafe_allow_html=True
        )
    with rc2:
        if not vad_awake:
            if st.button("👂 Wake", key="roux_wake", use_container_width=True,
                         help="Force Roux awake — skip wake word"):
                try:
                    requests.post(f"{ROUX_URL}/voice/vad/wake", timeout=3)
                except:
                    pass
                st.rerun()
        else:
            if st.button("😴 Sleep", key="roux_sleep", use_container_width=True,
                         help="Send Roux back to sleep"):
                try:
                    requests.post(f"{ROUX_URL}/voice/vad/sleep", timeout=3)
                except:
                    pass
                st.rerun()
    with rc3:
        if st.button("🔇 Stop VAD", key="roux_vad_stop", use_container_width=True,
                     help="Stop always-listening loop entirely"):
            try:
                requests.post(f"{ROUX_URL}/voice/vad/toggle", timeout=3)
            except:
                pass
            st.rerun()
    with rc4:
        timeout_s = vad.get("awake_timeout_s", 30)
        wake_word  = vad.get("wake_word", "roux")
        st.markdown(
            f'<span style="font-family:JetBrains Mono,monospace;font-size:0.6rem;color:#6b5f50">'
            f'wake: \'{wake_word}\' &nbsp;·&nbsp; timeout: {timeout_s}s</span>',
            unsafe_allow_html=True
        )


# --- KILL SWITCH (Phase 27) ---
KILL_SWITCH_URL = "http://localhost:8012"

def _render_kill_switch():
    """Render the kill switch status and toggle button."""
    try:
        r = requests.get(f"{KILL_SWITCH_URL}/kill-switch", timeout=2)
        if r.status_code == 200:
            state = r.json()
        else:
            return  # Can't reach watchtower cron
    except:
        return  # Service offline
    
    engaged = state.get("engaged", False)
    
    if engaged:
        reason = state.get("reason", "Manual")
        engaged_by = state.get("engaged_by", "unknown")
        engaged_at = state.get("engaged_at", 0)
        duration = time.time() - engaged_at if engaged_at else 0
        dur_str = f"{duration:.0f}s" if duration < 60 else f"{duration/60:.0f}m"
        
        st.markdown(
            f'<div style="background:rgba(139,58,58,0.2);border:2px solid #8b3a3a;'
            f'border-radius:12px;padding:0.75rem 1rem;margin-bottom:1rem;'
            f'display:flex;align-items:center;justify-content:space-between">'
            f'<div>'
            f'<span style="font-size:1.1rem;font-weight:700;color:#ef4444">'
            f'\U0001f6d1 KILL SWITCH ENGAGED</span><br>'
            f'<span style="color:#94a3b8;font-size:0.8rem">'
            f'{reason} (by {engaged_by}, {dur_str} ago) &mdash; '
            f'Task queue frozen, auto-approve disabled, no proposals dispatched</span>'
            f'</div>'
            f'</div>',
            unsafe_allow_html=True
        )
        if st.button("\u2705 Disengage Kill Switch", key="kill_switch_off", use_container_width=False):
            try:
                requests.post(f"{KILL_SWITCH_URL}/kill-switch/disengage", timeout=3)
            except:
                pass
            st.rerun()
    else:
        # Show a subtle engage button (not prominent when things are normal)
        ks_col1, ks_col2 = st.columns([6, 1])
        with ks_col2:
            if st.button("\U0001f6d1 Kill Switch", key="kill_switch_on", help="Emergency stop: freeze all autonomous execution"):
                try:
                    requests.post(
                        f"{KILL_SWITCH_URL}/kill-switch/engage",
                        json={"reason": "Dashboard manual engage", "engaged_by": "human"},
                        timeout=3
                    )
                except:
                    pass
                st.rerun()


def _render_budget_indicator():
    """Render execution budget status bar."""
    try:
        r = requests.get(f"{KILL_SWITCH_URL}/budget", timeout=2)
        if r.status_code != 200:
            return
        budget = r.json()
    except:
        return
    
    if not budget.get("enabled", True):
        return  # Budget tracking disabled
    
    used = budget.get("used_this_hour", 0)
    max_h = budget.get("max_per_hour", 20)
    remaining = budget.get("remaining", max_h)
    resets_in = budget.get("window_resets_in", 0)
    pct = min(100, (used / max(max_h, 1)) * 100)
    
    # Color: green < 50%, gold < 80%, red >= 80%
    if pct < 50:
        bar_color = "#3d6b48"  # moss
    elif pct < 80:
        bar_color = "#c89b3c"  # gold
    else:
        bar_color = "#8b3a3a"  # red
    
    reset_text = ""
    if resets_in > 0 and remaining == 0:
        reset_min = resets_in // 60
        reset_sec = resets_in % 60
        reset_text = f" &mdash; resets in {reset_min}m {reset_sec}s"
    
    st.markdown(
        f'<div style="display:flex;align-items:center;gap:0.75rem;margin-bottom:0.75rem">'
        f'<span style="font-family:JetBrains Mono,monospace;font-size:0.7rem;'
        f'color:var(--fog);text-transform:uppercase;letter-spacing:0.08em;min-width:130px">'
        f'\U0001f4ca Budget: {used}/{max_h}/hr</span>'
        f'<div style="flex:1;height:6px;background:rgba(100,116,139,0.15);border-radius:3px;overflow:hidden">'
        f'<div style="width:{pct}%;height:100%;background:{bar_color};border-radius:3px;'
        f'transition:width 0.3s ease"></div>'
        f'</div>'
        f'<span style="font-family:JetBrains Mono,monospace;font-size:0.65rem;'
        f'color:var(--text-muted);min-width:80px;text-align:right">'
        f'{remaining} remaining{reset_text}</span>'
        f'</div>',
        unsafe_allow_html=True
    )


# --- DEPLOY PANEL (Phase 20.2) ---
WATCHTOWER_DEPLOY_URL = "http://localhost:8010"

def fetch_pending_deploys():
    """Fetch pending deployments from Watchtower."""
    try:
        r = requests.get(f"{WATCHTOWER_DEPLOY_URL}/deploy/pending", timeout=3)
        if r.status_code == 200:
            return r.json().get("pending", [])
    except:
        pass
    return []

def fetch_deploy_history():
    """Fetch recent deploy history from Watchtower."""
    try:
        r = requests.get(f"{WATCHTOWER_DEPLOY_URL}/deploy/history", timeout=3)
        if r.status_code == 200:
            return r.json().get("deploys", [])
    except:
        pass
    return []

def render_deploy_panel():
    """Render deploy approval cards and history."""
    pending = fetch_pending_deploys()
    
    if not pending:
        return  # Nothing to show — keep the UI clean
    
    for deploy in pending:
        deploy_id = deploy.get("deploy_id", "")
        service = deploy.get("service", "unknown")
        version = deploy.get("version", "?")
        staging_port = deploy.get("staging_port", 0)
        staging_pid = deploy.get("staging_pid", 0)
        health = deploy.get("health_result", {})
        latency = health.get("latency_ms", "?") if health else "?"
        patch_count = len(deploy.get("patches", []))
        created = deploy.get("created_at", 0)
        
        time_str = datetime.fromtimestamp(created).strftime("%I:%M:%S %p") if created else ""
        
        # Build the approval card
        st.markdown(
            f'<div class="blocked-alert">'
            f'<div style="display:flex;align-items:center;justify-content:space-between">'
            f'<div>'
            f'<span style="font-size:1.1rem;font-weight:600;color:#f8fafc">'
            f'🚀 Deploy Awaiting Approval</span><br>'
            f'<span style="color:#94a3b8;font-size:0.85rem">'
            f'<b>{service}</b> v{version} &nbsp;•&nbsp; '
            f'Staging on :{staging_port} (PID {staging_pid}) &nbsp;•&nbsp; '
            f'Health: ✅ {latency}ms &nbsp;•&nbsp; '
            f'{patch_count} patch{"es" if patch_count != 1 else ""} &nbsp;•&nbsp; '
            f'{time_str}'
            f'</span>'
            f'</div>'
            f'</div>'
            f'<div style="font-family:monospace;font-size:0.7rem;color:#64748b;margin-top:0.5rem">'
            f'{deploy_id}'
            f'</div>'
            f'</div>',
            unsafe_allow_html=True
        )
        
        # Approve / Reject buttons
        btn_cols = st.columns([1, 1, 4])
        with btn_cols[0]:
            if st.button("✅ Approve & Swap", key=f"approve_{deploy_id}", use_container_width=True):
                try:
                    r = requests.post(f"{WATCHTOWER_DEPLOY_URL}/deploy/approve/{deploy_id}", timeout=30)
                    result = r.json()
                    if result.get("success"):
                        st.success(f"🎉 {result.get('message', 'Deploy complete!')}")
                    else:
                        st.error(f"❌ {result.get('error', 'Swap failed')}")
                except Exception as e:
                    st.error(f"Error: {e}")
                st.rerun()
        with btn_cols[1]:
            if st.button("❌ Reject", key=f"reject_{deploy_id}", use_container_width=True):
                try:
                    r = requests.post(f"{WATCHTOWER_DEPLOY_URL}/deploy/reject/{deploy_id}", timeout=10)
                    result = r.json()
                    if result.get("success"):
                        st.info(f"🚫 {result.get('message', 'Deploy rejected.')}")
                    else:
                        st.error(f"❌ {result.get('error', 'Reject failed')}")
                except Exception as e:
                    st.error(f"Error: {e}")
                st.rerun()


def format_time(ts):
    try:
        return datetime.fromtimestamp(ts).strftime("%H:%M")
    except:
        return ""

# --- CHAT / CONVERSATION FUNCTIONS ---
# Phase 20: All traffic routes through the Gateway
GATEWAY_URL = "http://localhost:8000"
COMPANION_URL = f"{GATEWAY_URL}/orch/companion"

# Import the unified conversation manager
import sys
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from shared.conversations import (
    create_conversation,
    get_active_conversation_id,
    set_active_conversation,
    list_conversations,
    search_conversations,
    delete_conversation,
    pin_conversation,
    add_message as conv_add_message,
    get_messages as conv_get_messages,
    generate_title as conv_generate_title,
    update_title as conv_update_title,
    migrate_existing_history,
)

# Run migration on first load (moves old chat_history.json / conversation.json)
migrate_existing_history()

def send_to_companion(message, confirmed=False, priority="normal"):
    """Send message to companion endpoint and get response"""
    try:
        payload = {"message": message, "priority": priority}
        if confirmed:
            payload["confirmed"] = True
        response = requests.post(
            COMPANION_URL,
            json=payload,
            timeout=180  # 3 min timeout for execution tasks
        )
        if response.status_code == 200:
            return response.json()
        else:
            return {"success": False, "response": f"Error: {response.status_code}"}
    except requests.exceptions.ConnectionError:
        return {"success": False, "response": "🔴 Gateway/Orchestrator offline. Start the agents first!"}
    except Exception as e:
        return {"success": False, "response": f"Error: {str(e)}"}

def _on_conv_select_change():
    """Callback when selectbox selection changes. Runs BEFORE the rerun."""
    # Skip if we're forcing a new conversation (New Chat button just pressed)
    if st.session_state.get("_force_conv"):
        return
    selected_label = st.session_state.get("conv_select")
    if selected_label is None:
        return
    # Look up the conversation ID from the label
    conv_map = st.session_state.get("_conv_label_map", {})
    selected_id = conv_map.get(selected_label)
    if selected_id:
        set_active_conversation(selected_id)


def render_conversation_bar():
    """Render the conversation selector strip above the chat."""
    convs = list_conversations(limit=30)
    active_id = get_active_conversation_id()
    
    # Build options for selectbox
    if not convs:
        conv_options = []
        conv_labels = []
    else:
        conv_options = [c["id"] for c in convs]
        conv_labels = []
        for c in convs:
            pin = "📌 " if c.get("pinned") else ""
            ts = datetime.fromtimestamp(c.get("updated_at", 0)).strftime("%b %d, %I:%M %p") if c.get("updated_at") else ""
            count = c.get("msg_count", 0)
            label = f"{pin}{c.get('title', 'Untitled')[:45]}  •  {ts}  ({count} msgs)"
            conv_labels.append(label)
    
    # Store label→id map in session state for the callback
    label_map = {}
    for label, cid in zip(conv_labels, conv_options):
        label_map[label] = cid
    st.session_state["_conv_label_map"] = label_map
    
    # Layout: [New Chat] [Conversation Dropdown] [Pin] [Delete]
    c1, c2, c3, c4 = st.columns([1, 4, 0.5, 0.5])
    
    with c1:
        if st.button("➕ New Chat", use_container_width=True, key="new_chat"):
            new_id = create_conversation()
            set_active_conversation(new_id)
            # Force selectbox to pick up new active on next render
            st.session_state["_force_conv"] = new_id
            # Clear stale selectbox state so on_change doesn't override
            if "conv_select" in st.session_state:
                del st.session_state["conv_select"]
            st.rerun()
    
    with c2:
        if conv_options:
            # If we just created a new chat, override the selectbox index
            forced_id = st.session_state.pop("_force_conv", None)
            if forced_id and forced_id in conv_options:
                current_idx = conv_options.index(forced_id)
            else:
                current_idx = conv_options.index(active_id) if active_id in conv_options else 0
            
            st.selectbox(
                "Conversation",
                options=conv_labels,
                index=current_idx,
                label_visibility="collapsed",
                key="conv_select",
                on_change=_on_conv_select_change,
            )
    
    with c3:
        if active_id and st.button("📌", use_container_width=True, key="pin_conv", help="Pin/unpin conversation"):
            current_conv = next((c for c in convs if c["id"] == active_id), None)
            if current_conv:
                pin_conversation(active_id, not current_conv.get("pinned", False))
                st.rerun()
    
    with c4:
        if active_id and st.button("🗑️", use_container_width=True, key="delete_conv", help="Delete conversation"):
            delete_conversation(active_id)
            remaining = list_conversations(limit=1)
            if remaining:
                set_active_conversation(remaining[0]["id"])
                st.session_state["_force_conv"] = remaining[0]["id"]
            else:
                new_id = create_conversation()
                st.session_state["_force_conv"] = new_id
            st.rerun()


def render_chat_panel():
    """Render the chat panel with conversation history and input.
    
    Phase 29.4 fix: Priority selectbox moved outside st.form to prevent
    form interaction bugs. Chat input no longer disrupted by auto-refresh
    (handled via st.fragment isolation in main()).
    """
    st.markdown('<div class="card-title">💬 Companion Chat</div>', unsafe_allow_html=True)
    
    # Conversation selector bar
    render_conversation_bar()
    
    # Initialize session state
    if "pending_confirmation" not in st.session_state:
        st.session_state.pending_confirmation = None
    
    # Get active conversation ID (also used by send handler below)
    active_id = get_active_conversation_id()
    
    # Chat messages — use fragment for isolation from auto-refresh so typing is never interrupted
    if _FRAGMENT_AVAILABLE:
        _chat_messages_fragment()
    else:
        # Fallback: inline rendering (Streamlit < 1.33)
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
                        del st.session_state["_awaiting_task"]
                        st.rerun()
                    elif task_state in ("queued", "running"):
                        task_is_running = True
                elif r.status_code == 404:
                    del st.session_state["_awaiting_task"]
                    st.rerun()
            except:
                pass
            if time.time() - awaiting.get("queued_at", 0) > 300:
                st.session_state.pop("_awaiting_task", None)
                task_is_running = False
        
        chat_html = '<div class="chat-container"><div class="chat-messages" id="chat-scroll">'
        if not history:
            chat_html += '<div class="chat-message assistant"><div class="chat-bubble">👋 Hey DJ! I\'m your Companion. Ask me anything or tell me what to do!</div></div>'
        for msg in history:
            role = msg.get("role", "user")
            content = html.escape(msg.get("content", ""))
            content = content.replace("\n", "<br>")
            ts = msg.get("timestamp", 0)
            time_str = datetime.fromtimestamp(ts).strftime("%H:%M") if ts else ""
            intent = msg.get("metadata", {}).get("intent", "")
            intent_badge = f'<div class="chat-intent">{intent}</div>' if intent and role == "assistant" else ""
            chat_html += f'<div class="chat-message {role}"><div class="chat-bubble">{content}</div><div class="chat-time">{time_str}</div>{intent_badge}</div>'
        if task_is_running:
            elapsed = time.time() - awaiting.get("queued_at", time.time())
            elapsed_str = f"{elapsed:.0f}s" if elapsed < 60 else f"{elapsed/60:.1f}m"
            chat_html += ('<div class="chat-message assistant"><div class="chat-bubble" style="opacity:0.7">'
                          f'<span class="chat-typing">Working on it ({elapsed_str})</span></div></div>')
        chat_html += '</div></div>'
        st.markdown(chat_html, unsafe_allow_html=True)
        
        import streamlit.components.v1 as components
        components.html("""
            <script>
                var chatDiv = window.parent.document.getElementById('chat-scroll');
                if (chatDiv) chatDiv.scrollTop = chatDiv.scrollHeight;
            </script>
        """, height=0)
    
    # Confirmation buttons (if pending)
    if st.session_state.pending_confirmation:
        pending = st.session_state.pending_confirmation
        conf_cols = st.columns([1, 1, 3])
        with conf_cols[0]:
            if st.button("✅ Yes, proceed", key="confirm_yes", use_container_width=True):
                with st.spinner("⚡ Executing..."):
                    result = send_to_companion(pending["original_request"], confirmed=True)
                st.session_state.pending_confirmation = None
                st.rerun()
        with conf_cols[1]:
            if st.button("❌ Cancel", key="confirm_no", use_container_width=True):
                # Save cancel messages directly (orchestrator won't see these)
                conv_add_message("user", "No, cancel", conv_id=active_id)
                conv_add_message("assistant", "Got it, cancelled. 👍", conv_id=active_id)
                st.session_state.pending_confirmation = None
                st.rerun()
    
    # Priority selector lives OUTSIDE the form to avoid Streamlit form interaction bugs.
    # (Selectbox inside st.form causes focus issues + flaky behavior with auto-refresh)
    p_col1, p_col2 = st.columns([6, 2])
    with p_col2:
        priority_choice = st.selectbox(
            "Priority",
            ["🟡 Normal", "⚡ Urgent", "⚪ Background"],
            index=0,
            label_visibility="collapsed",
            key="priority_select"
        )
    
    # Map UI label to API value
    _priority_map = {"🟡 Normal": "normal", "⚡ Urgent": "urgent", "⚪ Background": "background"}
    selected_priority = _priority_map.get(priority_choice, "normal")
    
    # Chat input — form allows Enter key to submit (selectbox removed, no more focus theft)
    with st.form("chat_form", clear_on_submit=True):
        col1, col2 = st.columns([6, 1])
        with col1:
            chat_input = st.text_input(
                "Message",
                placeholder="Talk to me... (ask questions, give commands, or just chat!)",
                label_visibility="collapsed",
                key="chat_input"
            )
        with col2:
            send_clicked = st.form_submit_button("📨 Send", use_container_width=True)
    
    # Handle send
    if send_clicked and chat_input:
        # NOTE: Orchestrator handles saving both user + assistant messages
        # via companion.py -> conversations.py. Dashboard only reads.
        
        # Get companion response (orchestrator saves messages internally)
        with st.spinner("🤔 Thinking..."):
            result = send_to_companion(chat_input, priority=selected_priority)
        
        # Check if this needs confirmation
        if result.get("needs_confirmation"):
            st.session_state.pending_confirmation = {
                "original_request": result.get("original_request", chat_input)
            }
        
        # If task was queued, store task_id for completion polling
        if result.get("queued") and result.get("task_id"):
            st.session_state["_awaiting_task"] = {
                "task_id": result["task_id"],
                "query": chat_input,
                "queued_at": time.time(),
            }
        
        # Auto-generate title after 3rd user message using Ministral
        msg_count = len([m for m in conv_get_messages(limit=100, conv_id=active_id) if m["role"] == "user"])
        if msg_count == 3:
            import asyncio
            try:
                loop = asyncio.new_event_loop()
                loop.run_until_complete(conv_generate_title(active_id))
                loop.close()
            except Exception:
                pass  # Title generation is best-effort
        
        # Refresh to show new messages
        st.rerun()

# --- Phase 19: QUEUE PANEL ---
QUEUE_URL = f"{GATEWAY_URL}/orch/queue"

def fetch_queue_state():
    """Fetch live queue state from Orchestrator."""
    try:
        r = requests.get(QUEUE_URL, timeout=3)
        if r.status_code == 200:
            return r.json()
    except:
        pass
    return None

HISTORY_URL = f"{GATEWAY_URL}/orch/queue/history"

def _priority_badge(label):
    return {"urgent": "🔴", "normal": "🟡", "background": "⚪"}.get(label or "", "🟡")

def _fmt_duration(started, completed):
    if started and completed:
        d = completed - started
        if d < 60:
            return f"{d:.1f}s"
        return f"{d/60:.1f}m"
    return ""

def _fmt_timestamp(ts):
    try:
        return datetime.fromtimestamp(ts).strftime("%b %d, %I:%M %p")
    except:
        return ""

def render_queue_panel():
    """Phase 19.1: Polished live queue view with priority badges and history."""
    qs = fetch_queue_state()
    
    if qs is None:
        st.warning("🔴 Cannot reach Orchestrator queue. Is it running?")
        return
    
    stats = qs.get("stats", {})
    current = qs.get("current")
    pending = qs.get("pending", [])
    
    # Metrics row
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Queued", stats.get("pending_count", 0))
    is_paused = stats.get("is_paused", False)
    if is_paused:
        m2.metric("Processing", "⏸️ Paused")
    elif stats.get("is_processing"):
        m2.metric("Processing", "▶️ Active")
    else:
        m2.metric("Processing", "— Idle")
    m3.metric("Completed", stats.get("total_completed", 0))
    m4.metric("Failed", stats.get("total_failed", 0))
    
    # Control buttons: Pause/Resume + Abort
    ctrl1, ctrl2, ctrl3 = st.columns([1, 1, 4])
    with ctrl1:
        if is_paused:
            if st.button("▶️ Resume", use_container_width=True, key="q_resume"):
                try:
                    requests.post(f"{GATEWAY_URL}/orch/queue/resume", timeout=3)
                except:
                    pass
                st.rerun()
        else:
            if st.button("⏸️ Pause", use_container_width=True, key="q_pause"):
                try:
                    requests.post(f"{GATEWAY_URL}/orch/queue/pause", timeout=3)
                except:
                    pass
                st.rerun()
    with ctrl2:
        if current:
            if st.button("🛑 Abort Task", use_container_width=True, key="q_abort"):
                try:
                    requests.post(f"{GATEWAY_URL}/orch/queue/abort", timeout=3)
                except:
                    pass
                st.rerun()
    
    # Currently running task (highlighted card)
    if current:
        elapsed = time.time() - current.get("started_at", time.time())
        badge = _priority_badge(current.get("priority_label"))
        st.markdown(
            f'<div class="bento-card active">'
            f'<span class="card-title">▶️ Running — {elapsed:.0f}s</span>'
            f'<div style="margin-top:0.5rem">{badge} <code>#{current["id"]}</code> — {html.escape(current["query"][:120])}</div>'
            f'</div>',
            unsafe_allow_html=True
        )
    
    # Pending queue with cancel buttons
    if pending:
        st.markdown(f"**⏳ Pending ({len(pending)})**")
        for task in pending:
            c1, c2 = st.columns([6, 1])
            with c1:
                badge = _priority_badge(task.get("priority_label"))
                age = time.time() - task.get("created_at", time.time())
                age_str = f"{age:.0f}s ago" if age < 60 else f"{age/60:.0f}m ago"
                st.markdown(f"{badge} `#{task['id']}` — {task['query'][:70]}")
                st.caption(f"Queued {age_str}")
            with c2:
                if st.button("🗑️", key=f"cancel_{task['id']}", help="Cancel task"):
                    try:
                        requests.delete(f"{QUEUE_URL}/{task['id']}", timeout=3)
                    except:
                        pass
                    st.rerun()
    elif not current:
        st.info("✨ Queue empty. Send tasks via the Chat tab!")
    
    st.markdown("---")
    
    # Persistent history (from disk, supports pagination)
    # Archive controls
    hist_ctrl1, hist_ctrl2, hist_ctrl3 = st.columns([1, 1, 4])
    with hist_ctrl1:
        hide_archived = st.toggle("🙈 Hide Archived", value=False, key="hide_archived")
    with hist_ctrl2:
        if st.button("📦 Archive All", key="archive_all", help="Archive all visible history"):
            try:
                requests.post(f"{GATEWAY_URL}/orch/queue/archive-all", timeout=5)
            except:
                try:
                    requests.post("http://localhost:8001/queue/archive-all", timeout=5)
                except:
                    pass
            st.rerun()
    
    full_history = []
    hist_params = {"limit": 50, "include_archived": "true"}
    if hide_archived:
        hist_params.pop("include_archived")
    try:
        hist_resp = requests.get(HISTORY_URL, params=hist_params, timeout=5)
        if hist_resp.status_code == 200:
            full_history = hist_resp.json().get("tasks", [])
    except Exception:
        pass
    # Fallback: try direct orchestrator if gateway proxy failed
    if not full_history:
        try:
            direct_resp = requests.get("http://localhost:8001/queue/history", params=hist_params, timeout=5)
            if direct_resp.status_code == 200:
                full_history = direct_resp.json().get("tasks", [])
        except Exception:
            pass
    # Final fallback: in-memory from queue state
    # Note: in-memory fallback doesn't support archive filtering,
    # so only use it when show_archived is True (show everything)
    if not full_history:
        mem_history = qs.get("history", [])
        if hide_archived:
            full_history = [t for t in mem_history if not t.get("archived", False)]
        else:
            full_history = mem_history
    
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

            # Phase 39: Verification badge — single character indicating drift verdict
            result_for_badge = task.get("result") or {}
            v_result = result_for_badge.get("verification_result") or {}
            v_verdict = v_result.get("verdict")
            if v_verdict == "pass":
                v_badge = "✓ "
            elif v_verdict == "fail":
                v_badge = "⚠ "
            elif v_verdict == "uncertain":
                v_badge = "? "
            else:
                v_badge = ""

            with st.expander(
                f"{v_badge}{emoji} {badge} {task['query'][:60]}{'...' if len(task.get('query',''))>60 else ''}"
                f"  —  {dur}  •  {ts}{archive_label}",
                expanded=False
            ):
                st.caption(f"ID: `{task['id']}` • Priority: {task.get('priority_label', 'normal')} • State: {state}")
                st.markdown(f"**Query:** {task['query']}")
                if task.get("error"):
                    st.error(f"Error: {task['error'][:300]}")
                # Show synthesized response if available
                result = task.get("result", {})
                if isinstance(result, dict) and result.get("synthesized_response"):
                    st.markdown(f"**Result:** {result['synthesized_response'][:500]}")

                # Phase 39: Verification block — show drift verdict + missing constraints
                if isinstance(result, dict):
                    vr = result.get("verification_result") or {}
                    vv = vr.get("verdict")
                    if vv and vv != "skipped":
                        v_reason = vr.get("reason", "")
                        v_missing = vr.get("missing_constraints", []) or []
                        if vv == "pass":
                            st.success(f"**Verifier:** {v_reason[:400]}")
                        elif vv == "fail":
                            missing_md = "\n".join(f"- {m}" for m in v_missing) or "- (none listed)"
                            st.error(
                                f"**Verifier flagged drift:** {v_reason[:400]}\n\n"
                                f"**Missing constraints:**\n{missing_md}"
                            )
                        elif vv == "uncertain":
                            st.warning(f"**Verifier uncertain:** {v_reason[:400]}")
                # Archive / Unarchive button
                task_id = task.get("id", "")
                if is_archived:
                    if st.button("📤 Unarchive", key=f"unarchive_{task_id}"):
                        try:
                            requests.post(f"{GATEWAY_URL}/orch/queue/{task_id}/unarchive", timeout=5)
                        except:
                            try:
                                requests.post(f"http://localhost:8001/queue/{task_id}/unarchive", timeout=5)
                            except:
                                pass
                        st.rerun()
                else:
                    if st.button("📦 Archive", key=f"archive_{task_id}"):
                        try:
                            requests.post(f"{GATEWAY_URL}/orch/queue/{task_id}/archive", timeout=5)
                        except:
                            try:
                                requests.post(f"http://localhost:8001/queue/{task_id}/archive", timeout=5)
                            except:
                                pass
                        st.rerun()
    else:
        if hide_archived:
            st.caption("No unarchived tasks. Toggle 'Hide Archived' off to see all history.")
        else:
            st.caption("No task history yet.")


# --- PROPOSALS PANEL (Phase 25) ---
WATCHTOWER_CRON_URL = "http://localhost:8012"

def fetch_proposals():
    """Trigger proposer and get fresh proposals."""
    try:
        r = requests.post(f"{WATCHTOWER_CRON_URL}/proposals", timeout=120)
        if r.status_code == 200:
            return r.json()
        else:
            st.caption(f"⚠️ Proposer returned HTTP {r.status_code}")
    except requests.exceptions.Timeout:
        st.caption("⏳ Proposer scan timed out (still running)...")
    except requests.exceptions.ConnectionError:
        pass  # Cron service not running — handled by caller
    except Exception as e:
        st.caption(f"⚠️ Proposer error: {e}")
    return None

def _cat_color(category):
    return {
        "health": "#ef4444", "memory": "#8b5cf6", "codebase": "#3b82f6",
        "tasks": "#f59e0b", "resources": "#f97316", "skills": "#10b981",
        "optimization": "#06b6d4",
    }.get(category, "#64748b")

def _cat_emoji(category):
    return {
        "health": "🏥", "memory": "🧠", "codebase": "📦",
        "tasks": "📋", "resources": "💾", "skills": "⚡",
        "optimization": "🔬",
    }.get(category, "❓")

def render_proposals_panel():
    """Phase 25.2: Proposals panel with auto-approve toggle, notifications, and badges."""
    import streamlit.components.v1 as components

    # Show toast from previous action (survives rerun via session_state)
    approved = st.session_state.pop("_proposal_approved", None)
    if approved:
        st.success(f"✅ Added to task queue: {approved}")
    dismissed = st.session_state.pop("_proposal_dismissed", None)
    if dismissed:
        st.info(f"🗑️ Dismissed: {dismissed}. Won't re-propose for 12h.")

    # Phase 25.2b: Request notification permission on first load
    if "notif_permission_asked" not in st.session_state:
        components.html("""
        <script>
        if ("Notification" in window && Notification.permission === "default") {
            Notification.requestPermission();
        }
        </script>
        """, height=0)
        st.session_state["notif_permission_asked"] = True

    # Controls row with auto-approve toggle
    ctrl1, ctrl2, ctrl3, ctrl4 = st.columns([1, 1, 1.5, 2.5])
    with ctrl1:
        manual_scan = st.button("🔍 Scan Now", use_container_width=True, key="proposer_scan")
    with ctrl2:
        st.caption("🔄 Auto-scans every 30min via cron")

    # Phase 25.2a: Auto-approve controls
    _aa_config = {}
    try:
        _aa_resp = requests.get(f"{WATCHTOWER_CRON_URL}/auto-approve/config", timeout=3)
        if _aa_resp.status_code == 200:
            _aa_config = _aa_resp.json().get("config", {})
    except:
        pass

    with ctrl3:
        _aa_enabled = _aa_config.get("enabled", True)
        _aa_toggle = st.toggle(
            "⚡ Auto-Approve",
            value=_aa_enabled,
            key="auto_approve_toggle",
            help="Auto-approve low-risk, reversible proposals (health, memory, resources only)"
        )

        if _aa_toggle != _aa_enabled:
            try:
                requests.post(
                    f"{WATCHTOWER_CRON_URL}/auto-approve/config",
                    json={"enabled": _aa_toggle},
                    timeout=3
                )
            except:
                pass

    with ctrl4:
        if _aa_config:
            _daily_used = _aa_config.get("today_count", 0)
            _daily_limit = _aa_config.get("daily_limit", 10)

            new_limit = st.slider(
                "Daily limit",
                min_value=1, max_value=50, value=_daily_limit,
                key="auto_approve_limit",
                help=f"Max auto-approvals per day. Today: {_daily_used}/{_daily_limit}",
                label_visibility="collapsed",
            )
            st.caption(f"🤖 {_daily_used}/{_daily_limit} auto-approved today")

            if new_limit != _daily_limit:
                try:
                    requests.post(
                        f"{WATCHTOWER_CRON_URL}/auto-approve/config",
                        json={"daily_limit": new_limit},
                        timeout=3
                    )
                except:
                    pass

    # Fetch proposals
    data = None

    if manual_scan:
        with st.spinner("🔍 Running proposer scan + Coach enrichment (may take ~45s)..."):
            data = fetch_proposals()

    if data is None:
        try:
            r = requests.get(f"{WATCHTOWER_CRON_URL}/proposals", timeout=5)
            if r.status_code == 200:
                data = r.json()
        except requests.exceptions.ConnectionError:
            pass
        except Exception as e:
            st.caption(f"⚠️ Error fetching proposals: {e}")

    if data is None:
        st.warning("🔴 Cannot reach Watchtower Cron (port 8012). Start it with: python services\\watchtower\\api.py")
        return

    raw_proposals = data.get("proposals", [])
    observer_stats = data.get("observer_stats", {})

    if isinstance(raw_proposals, dict):
        proposals = []
    elif isinstance(raw_proposals, list):
        proposals = [p for p in raw_proposals if isinstance(p, dict)]
    else:
        proposals = []

    # Phase 25.2b: Fire notifications for new high-priority or auto-approved proposals
    if proposals:
        seen = st.session_state.get("seen_proposals", set())
        notify_items = [
            p for p in proposals
            if p.get("id") not in seen
            and (
                (p.get("priority", 0) >= 8 and p.get("state") == "pending")
                or p.get("approved_by") == "auto"
            )
        ]

        if notify_items:
            for np in notify_items:
                prefix = "🤖 Auto" if np.get("approved_by") == "auto" else f"🔔 P{np['priority']}"
                title_safe = np.get("title", "")[:60].replace("'", "\\'").replace('"', '\\"')
                components.html(f"""
                <script>
                if ("Notification" in window && Notification.permission === "granted") {{
                    new Notification("RouxYou", {{
                        body: "{prefix}: {title_safe}",
                        tag: "{np['id']}"
                    }});
                }}
                </script>
                """, height=0)

            seen.update(p["id"] for p in notify_items)
            st.session_state["seen_proposals"] = seen

    # Observer status summary
    obs_cols = st.columns(6)
    obs_names = ["health", "memory", "codebase", "tasks", "resources", "skills"]
    for col, name in zip(obs_cols, obs_names):
        with col:
            count = observer_stats.get(name, 0)
            emoji = _cat_emoji(name)
            if isinstance(count, int) and count > 0:
                st.markdown(
                    f'<div style="text-align:center;padding:0.3rem;'
                    f'background:rgba({"239,68,68" if name=="health" else "139,92,246"},0.15);'
                    f'border-radius:8px">'
                    f'<div style="font-size:1.2rem">{emoji}</div>'
                    f'<div style="color:#f8fafc;font-size:0.75rem;font-weight:600">{count}</div>'
                    f'<div style="color:#64748b;font-size:0.6rem;text-transform:uppercase">{name}</div>'
                    f'</div>',
                    unsafe_allow_html=True
                )
            else:
                st.markdown(
                    f'<div style="text-align:center;padding:0.3rem;'
                    f'background:rgba(100,116,139,0.08);border-radius:8px">'
                    f'<div style="font-size:1.2rem">{emoji}</div>'
                    f'<div style="color:#10b981;font-size:0.75rem">✓</div>'
                    f'<div style="color:#64748b;font-size:0.6rem;text-transform:uppercase">{name}</div>'
                    f'</div>',
                    unsafe_allow_html=True
                )

    st.markdown("<br>", unsafe_allow_html=True)

    # Proposals list
    if not proposals:
        st.markdown(
            '<div class="proposals-empty">'
            '<div class="proposals-empty-icon">✅</div>'
            '<div class="proposals-empty-text">System looks healthy</div>'
            '<div class="proposals-empty-sub">No proposals — all 6 observers found nothing to flag</div>'
            '</div>',
            unsafe_allow_html=True
        )
        return

    st.markdown(
        f'<div style="color:#f8fafc;font-weight:600;font-size:0.9rem;margin-bottom:0.75rem">'
        f'🔔 {len(proposals)} Proposal{"s" if len(proposals) != 1 else ""}</div>',
        unsafe_allow_html=True
    )

    for i, p in enumerate(proposals):
        cat = p.get("category", "")
        priority = p.get("priority", 0)
        reversible = p.get("reversible", True)
        title = html.escape(p.get("title", ""))
        desc = html.escape(p.get("description", ""))
        action = html.escape(p.get("proposed_action", ""))
        evidence = html.escape(p.get("evidence", ""))
        rev_class = "reversible" if reversible else "irreversible"
        rev_label = "↩ reversible" if reversible else "⚠ irreversible"
        state = p.get("state", "pending")
        source = p.get("source", "heuristic")
        executor_label = p.get("executor", "manual")

        # State badge styling
        state_colors = {
            "pending": ("#f59e0b", "⏳"), "approved": ("#8b5cf6", "✅"),
            "executing": ("#3b82f6", "⚡"), "completed": ("#10b981", "✔️"),
            "failed": ("#ef4444", "❌"), "dismissed": ("#64748b", "🚫"),
        }
        s_color, s_icon = state_colors.get(state, ("#64748b", "❓"))

        # Phase 25.1d: Coach enrichment badges
        confidence = p.get("confidence", None)
        coach_reasoning = p.get("coach_reasoning", "")

        conf_html = ""
        if confidence is not None and source == "coach":
            conf_pct = int(confidence * 100)
            conf_color = "#10b981" if confidence > 0.7 else ("#f59e0b" if confidence > 0.4 else "#ef4444")
            conf_html = (
                f'<span style="font-size:0.65rem;padding:0.15rem 0.4rem;border-radius:4px;'
                f'background:{conf_color}22;color:{conf_color};border:1px solid {conf_color}44;'
                f'margin-left:0.25rem">🎯 {conf_pct}%</span>'
            )

        source_html = ""
        if source == "coach":
            source_html = (
                '<span style="font-size:0.6rem;padding:0.12rem 0.35rem;border-radius:4px;'
                'background:rgba(200,155,60,0.12);color:#c89b3c;border:1px solid rgba(200,155,60,0.25);'
                'margin-left:0.25rem;font-family:JetBrains Mono,monospace">🧠 Coach</span>'
            )
        elif source == "research":
            source_html = (
                '<span style="font-size:0.6rem;padding:0.12rem 0.35rem;border-radius:4px;'
                'background:rgba(42,72,88,0.2);color:#2a4858;border:1px solid rgba(42,72,88,0.3);'
                'margin-left:0.25rem;font-family:JetBrains Mono,monospace">🔬 Research</span>'
            )

        # Phase 25.2a: Approved-by badge (auto vs human)
        approved_by = p.get("approved_by", "")
        approved_html = ""
        if approved_by == "auto":
            approved_html = '<span class="auto-badge auto-approved" style="margin-left:0.25rem">🤖 Auto</span>'
        elif approved_by == "human":
            approved_html = '<span class="auto-badge human-approved" style="margin-left:0.25rem">👤 Human</span>'

        reasoning_html = ""
        if coach_reasoning:
            reasoning_safe = html.escape(coach_reasoning)
            reasoning_html = (
                f'<div style="color:var(--gold-dim);font-size:0.7rem;font-style:italic;margin-top:0.25rem;'
                f'padding-left:0.5rem;border-left:2px solid rgba(200,155,60,0.3)">'
                f'🧠 {reasoning_safe}</div>'
            )

        # Render the card
        st.markdown(
            f'<div class="proposal-card cat-{cat}">'
            f'<div style="display:flex;justify-content:space-between;align-items:center">'
            f'<div class="proposal-title">{_cat_emoji(cat)} {title}</div>'
            f'<div>'
            f'{source_html}{conf_html}{approved_html}'
            f'<span style="font-size:0.65rem;padding:0.15rem 0.4rem;border-radius:4px;'
            f'background:{s_color}22;color:{s_color};border:1px solid {s_color}44;'
            f'margin-left:0.25rem">{s_icon} {state}</span>'
            f'</div>'
            f'</div>'
            f'<div class="proposal-desc">{desc}</div>'
            f'{reasoning_html}'
            f'<div class="proposal-meta">'
            f'<span class="proposal-badge priority">P{priority}</span>'
            f'<span class="proposal-badge category">{cat}</span>'
            f'<span class="proposal-badge {rev_class}">{rev_label}</span>'
            f'<span style="font-size:0.65rem;color:#64748b">→ {executor_label}</span>'
            f'</div>'
            f'<div class="proposal-action">→ {action}</div>'
            f'<div class="proposal-evidence">Evidence: {evidence}</div>'
            f'</div>',
            unsafe_allow_html=True
        )

        # Approve / Dismiss buttons (only for pending proposals)
        prop_id = p.get("id", f"unknown_{i}")
        executor = p.get("executor", "manual")
        if state != "pending":
            continue
        btn1, btn2, btn3 = st.columns([1, 1, 4])
        with btn1:
            if st.button("✅ Approve", key=f"approve_{prop_id}", use_container_width=True):
                try:
                    requests.post(f"{WATCHTOWER_CRON_URL}/proposals/{prop_id}/approve", timeout=5)
                except:
                    pass

                orch_payload = {
                    "proposal_id": prop_id,
                    "title": p.get("title", ""),
                    "description": p.get("description", ""),
                    "category": cat,
                    "priority": priority,
                    "proposed_action": p.get("proposed_action", ""),
                    "executor": executor,
                    "executor_meta": p.get("executor_meta", {}),
                }

                spinner_msg = {
                    "watchtower": f"🔄 Orchestrator dispatching restart...",
                    "coder": f"🧠 Orchestrator queuing for Coder...",
                    "worker": f"⚙️ Orchestrator queuing for Worker...",
                    "manual": f"📝 Acknowledging...",
                }.get(executor, "⚡ Processing...")

                with st.spinner(spinner_msg):
                    try:
                        r = requests.post(
                            f"{GATEWAY_URL}/orch/queue/proposal",
                            json=orch_payload,
                            timeout=30,
                        )
                        result = r.json()
                        if result.get("success"):
                            if result.get("queued"):
                                st.session_state["_proposal_approved"] = f"Queued as task #{result.get('task_id', '?')} ({result.get('priority', 'normal')})"
                            elif result.get("state") == "completed":
                                svc = p.get("executor_meta", {}).get("service_name", "")
                                startup = result.get("result", {}).get("startup_time", "?")
                                st.session_state["_proposal_approved"] = f"{svc} restarted! ({startup}s)" if svc else "Completed!"
                            else:
                                st.session_state["_proposal_approved"] = f"Approved: {p.get('title', '')[:50]}"
                        else:
                            st.session_state["_proposal_approved"] = f"⚠️ {result.get('error', 'Failed')}"
                    except requests.exceptions.ConnectionError:
                        st.session_state["_proposal_approved"] = "❌ Orchestrator offline — cannot execute proposal"
                    except Exception as e:
                        st.session_state["_proposal_approved"] = f"❌ Error: {e}"
                st.rerun()
        with btn2:
            if st.button("🗑️ Dismiss", key=f"dismiss_{prop_id}", use_container_width=True):
                try:
                    requests.post(f"{WATCHTOWER_CRON_URL}/proposals/{prop_id}/dismiss", timeout=5)
                except:
                    pass
                st.session_state["_proposal_dismissed"] = p.get("title", "")[:50]
                st.rerun()


# --- SCHEDULE PANEL ---
SCHEDULER_URL = "http://localhost:8005"

def fetch_schedule_week(ref_date=None):
    """Fetch this week's schedule from the scheduler service."""
    try:
        params = {"ref_date": ref_date} if ref_date else {}
        r = requests.get(f"{SCHEDULER_URL}/schedule/week", params=params, timeout=5)
        if r.status_code == 200:
            return r.json()
    except:
        pass
    return None

def fetch_schedule_conflicts():
    """Fetch unresolved conflicts."""
    try:
        r = requests.get(f"{SCHEDULER_URL}/schedule/conflicts", timeout=3)
        if r.status_code == 200:
            return r.json()
    except:
        pass
    return {"conflicts": [], "count": 0}

def fetch_schedule_entries(start_date=None, end_date=None):
    """Fetch entries with optional date filter."""
    try:
        params = {}
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date
        r = requests.get(f"{SCHEDULER_URL}/schedule/entries", params=params, timeout=5)
        if r.status_code == 200:
            return r.json()
    except:
        pass
    return {"entries": [], "count": 0}

def add_schedule_nl(text):
    """Send natural language schedule input."""
    try:
        r = requests.post(f"{SCHEDULER_URL}/schedule/add", json={"text": text}, timeout=60)
        if r.status_code == 200:
            return r.json()
        else:
            return {"success": False, "message": f"Error: {r.status_code}"}
    except Exception as e:
        return {"success": False, "message": f"Scheduler offline: {e}"}

def _fmt_12h(time_24: str) -> str:
    """Convert '17:00' -> '5:00pm', '08:30' -> '8:30am'."""
    if not time_24 or ':' not in time_24:
        return time_24 or ''
    try:
        h, m = int(time_24.split(':')[0]), int(time_24.split(':')[1])
        period = 'am' if h < 12 else 'pm'
        h12 = h % 12 or 12
        return f"{h12}:{m:02d}{period}" if m else f"{h12}{period}"
    except:
        return time_24

def _source_color(source):
    """Color coding for schedule sources."""
    return {
        "encore": "#3b82f6",      # blue
        "revolution": "#f59e0b",   # amber
        "phantom": "#8b5cf6",      # purple
        "manual": "#6b7280",       # gray
        "school": "#10b981",       # green
        "family": "#ec4899",       # pink
    }.get(source, "#6b7280")

def _source_emoji(source):
    return {
        "encore": "🔵",
        "revolution": "🟡",
        "phantom": "🟣",
        "manual": "⚪",
        "school": "🟢",
        "family": "🩷",
    }.get(source, "⚪")

def _sync_revolution():
    """Trigger Revolution schedule sync."""
    try:
        r = requests.post(f"{SCHEDULER_URL}/schedule/sync/revolution", timeout=120)
        if r.status_code == 200:
            return r.json()
        return {"success": False, "details": {"error": f"HTTP {r.status_code}"}}
    except Exception as e:
        return {"success": False, "details": {"error": str(e)}}


def render_schedule_panel():
    """Render the unified schedule panel."""
    
    # Check if scheduler service is online
    sched_online = check_service(8005)
    if not sched_online:
        st.warning("🔴 Scheduler service offline (port 8005). Start it to view schedule.")
        return
    
    # --- Sync Controls ---
    sync_cols = st.columns([1, 1, 1, 3])
    with sync_cols[0]:
        if st.button("🔄 Sync Revolution", use_container_width=True, key="sync_rev"):
            with st.spinner("🔄 Syncing Revolution schedule..."):
                result = _sync_revolution()
            if result.get("success"):
                added = result.get("added", 0)
                updated = result.get("updated", 0)
                removed = result.get("removed", 0)
                conflicts = result.get("conflicts_found", 0)
                msg = f"+{added} new, ~{updated} updated, -{removed} removed"
                if conflicts:
                    msg += f", ⚠️ {conflicts} conflicts!"
                st.success(f"🔄 Revolution synced: {msg}")
            else:
                st.error(f"❌ Sync failed: {result.get('details', {}).get('error', 'Unknown')}")
            st.rerun()
    with sync_cols[1]:
        if st.button("🔵 Sync Encore", use_container_width=True, key="sync_enc"):
            with st.spinner("🔵 Syncing Encore (Playwright)..."):
                try:
                    r = requests.post(f"{SCHEDULER_URL}/schedule/sync/encore", timeout=300)
                    if r.status_code == 200:
                        data = r.json()
                        if data.get("success"):
                            msg = f"+{data.get('added',0)} new, ~{data.get('updated',0)} updated, -{data.get('removed',0)} removed"
                            st.success(f"🔵 Encore synced: {msg}")
                        else:
                            st.error(f"❌ {data.get('details', {}).get('error', 'Unknown')}")
                    else:
                        st.error(f"Sync failed: HTTP {r.status_code}")
                except Exception as e:
                    st.error(f"Sync failed: {e}")
            st.rerun()
    with sync_cols[2]:
        if st.button("🔄 Sync All", use_container_width=True, key="sync_all"):
            with st.spinner("🔄 Syncing all sources..."):
                try:
                    r = requests.post(f"{SCHEDULER_URL}/schedule/sync/all", timeout=120)
                    if r.status_code == 200:
                        st.success("🔄 All sources synced!")
                    else:
                        st.error(f"Sync failed: HTTP {r.status_code}")
                except Exception as e:
                    st.error(f"Sync failed: {e}")
            st.rerun()
    with sync_cols[3]:
        if st.button("📅 Push to GCal", use_container_width=True, key="sync_gcal"):
            with st.spinner("📅 Syncing to Google Calendar..."):
                try:
                    r = requests.post(f"{SCHEDULER_URL}/schedule/gcal/sync", timeout=120)
                    if r.status_code == 200:
                        data = r.json()
                        if data.get("success"):
                            msg = f"+{data.get('created',0)} created, ~{data.get('updated',0)} updated"
                            st.success(f"📅 Google Calendar: {msg}")
                        else:
                            st.error(f"❌ {data.get('error', 'Unknown')}")
                    else:
                        st.error(f"GCal sync failed: HTTP {r.status_code}")
                except Exception as e:
                    st.error(f"GCal sync failed: {e}")
            st.rerun()
    
    # --- Conflict Alert Banner ---
    conflict_data = fetch_schedule_conflicts()
    conflicts = conflict_data.get("conflicts", [])
    if conflicts:
        for c in conflicts:
            sev = c.get("severity", "full")
            icon = {"full": "🚨", "partial": "⚠️", "adjacent": "⏱️"}.get(sev, "⚠️")
            color = {"full": "#ef4444", "partial": "#f59e0b", "adjacent": "#3b82f6"}.get(sev, "#f59e0b")
            st.markdown(
                f'<div style="background:rgba({"239,68,68" if sev=="full" else "245,158,11"}, 0.15);'
                f'border:2px solid {color};border-radius:12px;padding:0.75rem 1rem;margin-bottom:0.5rem">'
                f'<span style="font-size:0.95rem;color:#f8fafc;font-weight:600">'
                f'{icon} CONFLICT — {c.get("date", "")}</span><br>'
                f'<span style="color:#94a3b8;font-size:0.85rem">{html.escape(c.get("description", ""))}</span>'
                f'</div>',
                unsafe_allow_html=True
            )
    
    # --- NL Input ---
    with st.form("schedule_form", clear_on_submit=True):
        sc1, sc2 = st.columns([5, 1])
        with sc1:
            sched_input = st.text_input(
                "Add to schedule",
                placeholder='e.g. "Working revolution 3/15, 3/17" or "Encore at Convention Center next Thursday 8am-5pm"',
                label_visibility="collapsed",
                key="sched_input"
            )
        with sc2:
            sched_submit = st.form_submit_button("📅 Add", use_container_width=True)
    
    if sched_submit and sched_input:
        with st.spinner("🤔 Parsing schedule..."):
            result = add_schedule_nl(sched_input)
        if result.get("success"):
            st.success(result.get("message", "Added!"))
            if result.get("conflicts"):
                for c in result["conflicts"]:
                    st.error(f"🚨 {c.get('description', 'Conflict detected!')}")
        else:
            st.error(result.get("message", "Failed to add schedule entries."))
        st.rerun()
    
    # --- Week Navigation ---
    nav1, nav2, nav3 = st.columns([1, 2, 1])
    
    if "sched_week_offset" not in st.session_state:
        st.session_state.sched_week_offset = 0
    
    with nav1:
        if st.button("◀ Prev Week", use_container_width=True, key="prev_week"):
            st.session_state.sched_week_offset -= 1
            st.rerun()
    with nav3:
        if st.button("Next Week ▶", use_container_width=True, key="next_week"):
            st.session_state.sched_week_offset += 1
            st.rerun()
    with nav2:
        if st.session_state.sched_week_offset != 0:
            if st.button("📍 This Week", use_container_width=True, key="this_week"):
                st.session_state.sched_week_offset = 0
                st.rerun()
    
    # Calculate reference date from offset
    from datetime import timedelta
    ref = datetime.now().date() + timedelta(weeks=st.session_state.sched_week_offset)
    ref_str = ref.isoformat()
    
    # Fetch week data
    week_data = fetch_schedule_week(ref_str)
    if not week_data:
        st.info("No schedule data available.")
        return
    
    week_of = week_data.get("week_of", "")
    through = week_data.get("through", "")
    
    # Week header
    try:
        wo = datetime.strptime(week_of, "%Y-%m-%d")
        th = datetime.strptime(through, "%Y-%m-%d")
        week_label = f"{wo.strftime('%b %d')} — {th.strftime('%b %d, %Y')}"
    except:
        week_label = f"{week_of} to {through}"
    
    total = week_data.get("total_entries", 0)
    st.markdown(
        f'<div style="text-align:center;color:#94a3b8;font-size:0.85rem;margin-bottom:1rem">'
        f'📅 <b style="color:#f8fafc">{week_label}</b> &nbsp;•&nbsp; {total} event{"s" if total != 1 else ""}'
        f'</div>',
        unsafe_allow_html=True
    )
    
    # --- Day-by-day grid ---
    days = week_data.get("days", {})
    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    today_str = datetime.now().date().isoformat()
    
    # Render as 7 columns
    day_cols = st.columns(7)
    for i, (day_date, entries) in enumerate(sorted(days.items())):
        with day_cols[i]:
            is_today = day_date == today_str
            try:
                d = datetime.strptime(day_date, "%Y-%m-%d")
                day_label = d.strftime("%a")
                day_num = d.strftime("%d")
            except:
                day_label = day_names[i] if i < 7 else "?"
                day_num = day_date[-2:]
            
            # Day header
            border = "border:2px solid #8b5cf6;" if is_today else "border:1px solid rgba(148,163,184,0.1);"
            bg = "background:rgba(139,92,246,0.1);" if is_today else "background:#1f2937;"
            
            day_html = (
                f'<div style="{bg}{border}border-radius:12px;padding:0.5rem;min-height:120px;text-align:center">'
                f'<div style="color:{"#8b5cf6" if is_today else "#94a3b8"};font-size:0.7rem;font-weight:600;text-transform:uppercase">{day_label}</div>'
                f'<div style="color:#f8fafc;font-size:1.3rem;font-weight:700;margin-bottom:0.5rem">{day_num}</div>'
            )
            
            if entries:
                for entry in entries:
                    src = entry.get("source", "manual")
                    color = _source_color(src)
                    emoji = _source_emoji(src)
                    employer = entry.get("employer", src.title())
                    t_start = _fmt_12h(entry.get("start_time") or "")
                    t_end = _fmt_12h(entry.get("end_time") or "")
                    if t_start and t_end:
                        time_str = f"{t_start}-{t_end}"
                    elif t_start:
                        time_str = f"{t_start} call"
                    else:
                        time_str = "All day"
                    title = entry.get("title") or ""
                    location = entry.get("location") or ""
                    
                    day_html += (
                        f'<div style="background:{color}22;border-left:3px solid {color};'
                        f'border-radius:4px;padding:0.3rem 0.4rem;margin-bottom:0.3rem;text-align:left">'
                        f'<div style="color:{color};font-size:0.65rem;font-weight:600">{emoji} {employer}</div>'
                    )
                    if title:
                        day_html += f'<div style="color:#94a3b8;font-size:0.6rem">{html.escape(title)}</div>'
                    if location:
                        day_html += f'<div style="color:#7c9a92;font-size:0.55rem">📍 {html.escape(location)}</div>'
                    day_html += f'<div style="color:#64748b;font-size:0.6rem">{time_str}</div></div>'
            else:
                day_html += '<div style="color:#475569;font-size:0.7rem;margin-top:1rem">—</div>'
            
            day_html += '</div>'
            st.markdown(day_html, unsafe_allow_html=True)
    
    # --- Upcoming entries (next 14 days) ---
    st.markdown("<br>", unsafe_allow_html=True)
    upcoming_start = datetime.now().date().isoformat()
    upcoming_end = (datetime.now().date() + timedelta(days=14)).isoformat()
    upcoming_data = fetch_schedule_entries(start_date=upcoming_start, end_date=upcoming_end)
    upcoming = upcoming_data.get("entries", [])
    
    if upcoming:
        st.markdown(f'<div class="card-title">📋 Upcoming ({len(upcoming)} events, next 14 days)</div>', unsafe_allow_html=True)
        for entry in upcoming:
            src = entry.get("source", "manual")
            color = _source_color(src)
            emoji = _source_emoji(src)
            employer = entry.get("employer", src.title())
            edate = entry.get("date", "")
            t_start = _fmt_12h(entry.get("start_time") or "")
            t_end = _fmt_12h(entry.get("end_time") or "")
            if t_start and t_end:
                time_str = f"{t_start}–{t_end}"
            elif t_start:
                time_str = f"{t_start} call"
            else:
                time_str = "All day"
            title = entry.get("title") or ""
            location = entry.get("location") or ""
            notes = entry.get("notes") or ""
            
            try:
                d = datetime.strptime(edate, "%Y-%m-%d")
                date_display = d.strftime("%a %b %d")
            except:
                date_display = edate
            
            detail_parts = [x for x in [title, location, notes] if x]
            detail_str = " • ".join(detail_parts) if detail_parts else ""
            
            st.markdown(
                f'<div style="background:#1f2937;border:1px solid rgba(148,163,184,0.1);border-left:3px solid {color};'
                f'border-radius:8px;padding:0.5rem 0.75rem;margin-bottom:0.4rem;display:flex;align-items:center;gap:0.75rem">'
                f'<div style="min-width:80px;color:#f8fafc;font-weight:600;font-size:0.8rem">{date_display}</div>'
                f'<div style="color:{color};font-weight:600;font-size:0.8rem;min-width:100px">{emoji} {employer}</div>'
                f'<div style="color:#94a3b8;font-size:0.8rem">{time_str}</div>'
                f'<div style="color:#64748b;font-size:0.75rem;flex:1">{html.escape(detail_str)}</div>'
                f'</div>',
                unsafe_allow_html=True
            )


# --- MAIN UI ---

# ─── Fragment-based auto-refresh (Streamlit 1.33+) ───────────────────────────
# By running status/logs inside @st.fragment(run_every=N), only those sections
# rerun on the timer — the chat form is NEVER touched by auto-refresh.
# Falls back gracefully to the old st_autorefresh approach on older Streamlit.

_FRAGMENT_AVAILABLE = hasattr(st, "fragment")

if _FRAGMENT_AVAILABLE:
    @st.fragment(run_every=3)
    def _chat_messages_fragment():
        """Auto-refreshes chat messages (task results, voice messages) every 3s.
        Lives OUTSIDE the input form — typing is never interrupted.
        """
        active_id = get_active_conversation_id()
        history = conv_get_messages(limit=50, conv_id=active_id)
        
        # Task completion polling
        task_is_running = False
        awaiting = st.session_state.get("_awaiting_task")
        if awaiting:
            task_id = awaiting["task_id"]
            try:
                r = requests.get(f"{GATEWAY_URL}/orch/queue/{task_id}", timeout=3)
                if r.status_code == 200:
                    task_state = r.json().get("state", "")
                    if task_state in ("completed", "failed", "cancelled"):
                        del st.session_state["_awaiting_task"]
                        st.rerun(scope="fragment")
                    elif task_state in ("queued", "running"):
                        task_is_running = True
                elif r.status_code == 404:
                    del st.session_state["_awaiting_task"]
                    st.rerun(scope="fragment")
            except:
                pass
            if time.time() - awaiting.get("queued_at", 0) > 300:
                st.session_state.pop("_awaiting_task", None)
                task_is_running = False
        
        # Chat messages HTML
        chat_html = '<div class="chat-container"><div class="chat-messages" id="chat-scroll">'
        if not history:
            chat_html += '<div class="chat-message assistant"><div class="chat-bubble">👋 Hey DJ! I\'m your Companion. Ask me anything or tell me what to do!</div></div>'
        for msg in history:
            role = msg.get("role", "user")
            content = html.escape(msg.get("content", ""))
            content = content.replace("\n", "<br>")
            ts = msg.get("timestamp", 0)
            time_str = datetime.fromtimestamp(ts).strftime("%H:%M") if ts else ""
            intent = msg.get("metadata", {}).get("intent", "")
            intent_badge = f'<div class="chat-intent">{intent}</div>' if intent and role == "assistant" else ""
            chat_html += f'<div class="chat-message {role}"><div class="chat-bubble">{content}</div><div class="chat-time">{time_str}</div>{intent_badge}</div>'
        if task_is_running:
            elapsed = time.time() - awaiting.get("queued_at", time.time())
            elapsed_str = f"{elapsed:.0f}s" if elapsed < 60 else f"{elapsed/60:.1f}m"
            chat_html += ('<div class="chat-message assistant"><div class="chat-bubble" style="opacity:0.7">'
                          f'<span class="chat-typing">Working on it ({elapsed_str})</span></div></div>')
        chat_html += '</div></div>'
        st.markdown(chat_html, unsafe_allow_html=True)
        
        # Auto-scroll
        import streamlit.components.v1 as _comp
        _comp.html("""<script>
            var c = window.parent.document.getElementById('chat-scroll');
            if (c) c.scrollTop = c.scrollHeight;
        </script>""", height=0)

    @st.fragment(run_every=5)
    def _status_fragment():
        """Auto-refreshing status bar + brain activity + event feed + cost + kill switch + budget."""
        activity = get_activity()
        is_active = activity.get("status") not in ["idle", "success", "failed"]
        dot_class = "heartbeat-dot active" if is_active else "heartbeat-dot"

        # Service status pills
        status_cols = st.columns(8)
        worker_port = 8003
        try:
            gw_routes = requests.get(f"{GATEWAY_URL}/gateway/routes", timeout=2).json()
            worker_port = gw_routes.get("routes", {}).get("/worker", {}).get("port", 8003)
        except:
            pass
        services_status = [
            ("Gateway", check_service(8000)),
            ("Orchestrator", check_service(8001)),
            ("Coder", check_service(8002)),
            (f"Worker :{worker_port}", check_service(worker_port)),
            ("Watchtower", check_service(8010)),
            ("Scheduler", check_service(8005)),
            ("Proxmox", check_proxmox()),
            ("SearXNG", check_searxng()),
        ]
        for col, (name, online) in zip(status_cols, services_status):
            with col:
                if online:
                    st.markdown(f'<span class="status-pill status-online">🟢 {name}</span>', unsafe_allow_html=True)
                else:
                    st.markdown(f'<span class="status-pill status-offline">🔴 {name}</span>', unsafe_allow_html=True)

        st.markdown('<div class="section-divider"></div>', unsafe_allow_html=True)
        render_brain_activity()

        # Phase 37b: Event Feed — live stream of typed events
        recent_events = st.session_state.get("_recent_events", [])
        if recent_events:
            st.markdown('<div class="card-title">📡 Event Feed</div>', unsafe_allow_html=True)
            render_event_feed(recent_events)

        # Phase 35b: Cost tracking bar
        render_cost_bar()

        # Phase 34b: LLM provider status
        render_llm_status()

        _render_roux_controls()
        _render_kill_switch()
        _render_budget_indicator()
        render_deploy_panel()

    @st.fragment(run_every=5)
    def _logs_fragment():
        """Auto-refreshing live logs section."""
        st.markdown('<div class="section-divider"></div>', unsafe_allow_html=True)
        st.markdown('<div class="card-title">📺 Live Logs</div>', unsafe_allow_html=True)
        log_tabs = st.tabs(["🎯 Orch", "🧠 Coder", "⚙️ Worker", "👁️ Watch", "🌐 Gateway"])
        with log_tabs[0]:
            st.markdown(f'<div class="log-viewer">{html.escape(read_log("orchestrator"))}</div>', unsafe_allow_html=True)
        with log_tabs[1]:
            st.markdown(f'<div class="log-viewer">{html.escape(read_log("coder"))}</div>', unsafe_allow_html=True)
        with log_tabs[2]:
            st.markdown(f'<div class="log-viewer">{html.escape(read_log("worker"))}</div>', unsafe_allow_html=True)
        with log_tabs[3]:
            st.markdown(f'<div class="log-viewer">{html.escape(read_log("watchtower"))}</div>', unsafe_allow_html=True)
        with log_tabs[4]:
            st.markdown(f'<div class="log-viewer">{html.escape(read_log("gateway"))}</div>', unsafe_allow_html=True)
        if st.button("🧹 Clear Logs", use_container_width=True, key="clear_logs_frag"):
            for svc in ["orchestrator", "coder", "worker", "watchtower", "gateway"]:
                log_file = LOGS_DIR / f"{svc}.log"
                if log_file.exists():
                    log_file.write_text("")
            st.success("Logs cleared!")


def render_invoice_panel():
    """Invoice management panel — generate, track, and update invoice status."""
    SCHEDULER_URL = "http://localhost:8005"

    st.markdown('<div class="card-title">🧾 Invoices</div>', unsafe_allow_html=True)

    # --- Generate Invoice ---
    with st.expander("➕ Generate New Invoice", expanded=False):
        # Fetch schedule entries for selection
        try:
            resp = requests.get(f"{SCHEDULER_URL}/schedule/entries", timeout=5)
            all_entries = resp.json().get("entries", []) if resp.ok else []
        except Exception:
            all_entries = []

        # Filter to Revolution only by default
        rev_entries = [e for e in all_entries if "revolution" in (e.get("employer") or "").lower()]
        options = {f"{e['date']} — {e.get('title', 'Untitled')} ({e.get('location', '')})": e["id"] for e in rev_entries}

        if options:
            selected_labels = st.multiselect(
                "Select schedule entries to invoice:",
                list(options.keys()),
                key="inv_entry_select",
            )
            selected_ids = [options[lbl] for lbl in selected_labels]
        else:
            st.warning("No Revolution schedule entries found. Sync schedule first.")
            selected_ids = []

        rate = st.number_input("Day rate ($)", value=400.0, step=50.0, key="inv_rate")
        notes = st.text_input("Notes (optional)", key="inv_notes")

        if st.button("🧾 Generate Invoice", disabled=not selected_ids, key="inv_generate_btn"):
            try:
                resp = requests.post(
                    f"{SCHEDULER_URL}/schedule/invoice/generate",
                    json={"entry_ids": selected_ids, "rate": rate, "notes": notes or None},
                    timeout=15,
                )
                if resp.ok:
                    data = resp.json()
                    st.success(f"✅ {data['invoice_number']} generated — ${data['total']:.2f} | {data['entry_count']} entries")
                    st.rerun()
                else:
                    st.error(f"Error: {resp.text}")
            except Exception as ex:
                st.error(f"Request failed: {ex}")

    st.markdown('<div class="section-divider"></div>', unsafe_allow_html=True)

    # --- Invoice List ---
    try:
        resp = requests.get(f"{SCHEDULER_URL}/schedule/invoice/list", timeout=5)
        invoices = resp.json().get("invoices", []) if resp.ok else []
    except Exception:
        invoices = []
        st.error("Scheduler offline — can't load invoices.")

    if not invoices:
        st.info("No invoices yet. Generate one above.")
        return

    STATUS_COLORS = {
        "draft":  ("#c89b3c", "📝"),
        "sent":   ("#2a9d8f", "📤"),
        "paid":   ("#3d6b48", "✅"),
        "void":   ("#8b3a3a", "🚫"),
    }

    for inv in invoices:
        num = inv.get("invoice_number", "?")
        client = inv.get("client", "")
        total = inv.get("total", 0)
        status = inv.get("status", "draft")
        inv_date = inv.get("invoice_date", "")
        service_dates = inv.get("service_dates", [])
        color, icon = STATUS_COLORS.get(status, ("#9a9484", "❓"))

        dates_str = ", ".join(service_dates) if service_dates else "—"

        with st.container():
            col_info, col_actions = st.columns([3, 2])
            with col_info:
                st.markdown(
                    f'<div style="padding:10px 0">'
                    f'<span style="font-family:JetBrains Mono,monospace;font-size:13px;color:#c89b3c">{num}</span>'
                    f'&nbsp;&nbsp;<span style="color:{color};font-size:12px">{icon} {status.upper()}</span><br>'
                    f'<span style="font-size:14px;color:#d4d0c8">{client}</span>'
                    f'&nbsp;&nbsp;<span style="color:#9a9484;font-size:13px">${total:.2f}</span><br>'
                    f'<span style="color:#6b5f50;font-size:11px">Invoiced: {inv_date} &nbsp;|&nbsp; Services: {dates_str}</span>'
                    f'</div>',
                    unsafe_allow_html=True,
                )
            with col_actions:
                btn_cols = st.columns(3)
                with btn_cols[0]:
                    if status == "draft" and st.button("📤 Sent", key=f"sent_{num}", use_container_width=True):
                        try:
                            requests.post(f"{SCHEDULER_URL}/schedule/invoice/status", params={"invoice_number": num, "status": "sent"}, timeout=5)
                            st.rerun()
                        except Exception as ex:
                            st.error(str(ex))
                with btn_cols[1]:
                    if status in ("draft", "sent") and st.button("✅ Paid", key=f"paid_{num}", use_container_width=True):
                        try:
                            requests.post(f"{SCHEDULER_URL}/schedule/invoice/status", params={"invoice_number": num, "status": "paid"}, timeout=5)
                            st.rerun()
                        except Exception as ex:
                            st.error(str(ex))
                with btn_cols[2]:
                    filename = inv.get("filename", "")
                    if filename:
                        try:
                            pdf_resp = requests.get(f"{SCHEDULER_URL}/schedule/invoice/download/{filename}", timeout=10)
                            if pdf_resp.ok:
                                st.download_button(
                                    "⬇️ PDF",
                                    data=pdf_resp.content,
                                    file_name=filename,
                                    mime="application/pdf",
                                    key=f"dl_{num}",
                                    use_container_width=True,
                                )
                        except Exception:
                            pass

        st.markdown('<div style="border-bottom:1px solid rgba(42,74,50,0.2);margin:4px 0"></div>', unsafe_allow_html=True)


def main():
    # Header (static — never needs auto-refresh)
    activity = get_activity()
    is_active = activity.get("status") not in ["idle", "success", "failed"]
    dot_class = "heartbeat-dot active" if is_active else "heartbeat-dot"
    st.markdown(f'<h1 class="main-header">RouxYou <span class="{dot_class}"></span></h1>', unsafe_allow_html=True)
    st.markdown('<p class="sub-header">Sovereign AI Infrastructure</p>', unsafe_allow_html=True)
    
    if _FRAGMENT_AVAILABLE:
        # ✅ Fragment mode: status/logs auto-refresh independently.
        # The chat form is NEVER in a rerun loop — typing works perfectly.
        _status_fragment()
    else:
        # ⚠️ Fallback: old full-page approach (Streamlit < 1.33)
        worker_port = 8003
        try:
            gw_routes = requests.get(f"{GATEWAY_URL}/gateway/routes", timeout=2).json()
            worker_port = gw_routes.get("routes", {}).get("/worker", {}).get("port", 8003)
        except:
            pass
        services_status = [
            ("Gateway", check_service(8000)),
            ("Orchestrator", check_service(8001)),
            ("Coder", check_service(8002)),
            (f"Worker :{worker_port}", check_service(worker_port)),
            ("Watchtower", check_service(8010)),
            ("Scheduler", check_service(8005)),
            ("Proxmox", check_proxmox()),
            ("SearXNG", check_searxng()),
        ]
        status_cols = st.columns(8)
        for col, (name, online) in zip(status_cols, services_status):
            with col:
                if online:
                    st.markdown(f'<span class="status-pill status-online">🟢 {name}</span>', unsafe_allow_html=True)
                else:
                    st.markdown(f'<span class="status-pill status-offline">🔴 {name}</span>', unsafe_allow_html=True)
        st.markdown('<div class="section-divider"></div>', unsafe_allow_html=True)
        render_brain_activity()
        # Phase 37b: Event Feed (fallback path)
        recent_events = st.session_state.get("_recent_events", [])
        if recent_events:
            st.markdown('<div class="card-title">📡 Event Feed</div>', unsafe_allow_html=True)
            render_event_feed(recent_events)
        # Phase 35b: Cost tracking bar (fallback path)
        render_cost_bar()
        # Phase 34b: LLM provider status (fallback path)
        render_llm_status()
        _render_roux_controls()
        _render_kill_switch()
        _render_budget_indicator()
        render_deploy_panel()

    # Mode Selection: Chat vs Schedule vs Queue+Proposals vs Invoices
    mode_tabs = st.tabs(["💬 Chat with Companion", "🗓️ Schedule", "📋 Task Queue", "🧾 Invoices"])

    # TAB 1: COMPANION CHAT
    with mode_tabs[0]:
        render_chat_panel()

    # TAB 2: SCHEDULE
    with mode_tabs[1]:
        render_schedule_panel()

    # TAB 3: UNIFIED QUEUE + PROPOSALS
    with mode_tabs[2]:
        render_queue_panel()
        st.markdown("<hr>", unsafe_allow_html=True)
        st.markdown('<div class="card-title">🔔 System Proposals</div>', unsafe_allow_html=True)
        render_proposals_panel()

    # TAB 4: INVOICES
    with mode_tabs[3]:
        render_invoice_panel()
    
    if _FRAGMENT_AVAILABLE:
        # Fragment handles logs with its own refresh loop
        _logs_fragment()
    else:
        # Fallback: static logs + page-level autorefresh
        st.markdown('<div class="section-divider"></div>', unsafe_allow_html=True)
        st.markdown('<div class="card-title">📺 Live Logs</div>', unsafe_allow_html=True)
        log_tabs = st.tabs(["🎯 Orch", "🧠 Coder", "⚙️ Worker", "👁️ Watch", "🌐 Gateway"])
        with log_tabs[0]:
            st.markdown(f'<div class="log-viewer">{html.escape(read_log("orchestrator"))}</div>', unsafe_allow_html=True)
        with log_tabs[1]:
            st.markdown(f'<div class="log-viewer">{html.escape(read_log("coder"))}</div>', unsafe_allow_html=True)
        with log_tabs[2]:
            st.markdown(f'<div class="log-viewer">{html.escape(read_log("worker"))}</div>', unsafe_allow_html=True)
        with log_tabs[3]:
            st.markdown(f'<div class="log-viewer">{html.escape(read_log("watchtower"))}</div>', unsafe_allow_html=True)
        with log_tabs[4]:
            st.markdown(f'<div class="log-viewer">{html.escape(read_log("gateway"))}</div>', unsafe_allow_html=True)
        if st.button("🧹 Clear Logs", use_container_width=True):
            for svc in ["orchestrator", "coder", "worker", "watchtower", "gateway"]:
                log_file = LOGS_DIR / f"{svc}.log"
                if log_file.exists():
                    log_file.write_text("")
            st.success("Logs cleared!")
        
        # Old auto-refresh — only fires in fallback mode
        from streamlit_autorefresh import st_autorefresh
        _awaiting_task = st.session_state.get("_awaiting_task")
        _refresh_ms = 5000 if (is_active or _awaiting_task) else 30000  # 30s idle in fallback
        st_autorefresh(interval=_refresh_ms, limit=None, key="mc_refresh")

if __name__ == "__main__":
    main()
