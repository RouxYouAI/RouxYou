"""
THE COMPANION
Conversational layer that makes the agent system feel human.

Responsibilities:
1. Intent Classification - What does the user want?
2. Response Synthesis - Turn execution results into natural conversation
3. Conversation Memory - Track the chat history
"""
import json
import re
import time
import aiohttp
from pathlib import Path
from typing import Optional, Dict, Any, List
from enum import Enum

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from config import CONFIG

from shared.conversations import (
    add_message as _conv_add_message,
    get_messages as _conv_get_messages,
    get_active_conversation_id,
    add_user_message,
    add_assistant_message,
    get_recent_messages,
)

OLLAMA_GENERATE_URL = f"{CONFIG.OLLAMA_HOST}/api/generate"
RAG_BRIDGE_URL = f"http://localhost:{CONFIG.PORT_RAG}"

SYSTEM_CAPABILITIES = """You are RouxYou — a fully local, self-evolving AI agent system.
You run entirely on the operator's hardware with zero cloud dependencies.
Your name comes from "roux" (the culinary foundation) + "you" (sovereign, runs on YOUR hardware).

You have REAL capabilities through your agent pipeline (Coder plans, Worker executes):

FILESYSTEM: Read, write, edit, copy, move, delete files. List directories. Search by filename or content.
WEB SEARCH: Search the internet via a self-hosted search engine. Returns real results with titles, snippets, URLs.
COMMAND EXECUTION: Run shell commands on the local machine.
HOME ASSISTANT: Toggle smart home devices via the HA API (if configured).
SELF-IMPROVEMENT: Proposal system with observers, LLM Coach, and auto-approve for safe self-healing.

IMPORTANT RULES:
- If a user asks you to do something you CAN do (list files, search the web, write code), classify it as execute or execute_explain.
- Only say "I can't" for things genuinely outside your capabilities (e.g., making phone calls, accessing hardware you're not connected to).
- If execution FAILED, say what went wrong — don't pretend you lack the ability."""


class Intent(Enum):
    CHAT = "chat"
    CHAT_INFORMED = "chat_informed"
    EXECUTE = "execute"
    EXECUTE_EXPLAIN = "execute_explain"
    CLARIFY = "clarify"
    CONFIRM = "confirm"


INTENT_PROMPT = """You are an intent classifier for an AI assistant with real system access.

{capabilities}

USER MESSAGE: "{user_input}"

RECENT CONTEXT (last few messages):
{recent_context}

Classify the intent and respond with ONLY valid JSON (no markdown, no explanation):

{{
    "intent": "chat" | "chat_informed" | "execute" | "execute_explain" | "clarify" | "confirm",
    "confidence": 0.0-1.0,
    "risk_level": "low" | "medium" | "high",
    "reasoning": "brief explanation of classification",
    "clarifying_question": "question to ask if intent is clarify",
    "task_summary": "what to execute if intent involves execution",
    "confirmation_message": "what to confirm if intent is confirm"
}}

CLASSIFICATION GUIDE:
- "chat": Pure casual conversation, jokes, small talk, greetings
- "chat_informed": Conversational question that benefits from checking memory/knowledge first
- "execute": Clear technical task, no need for detailed explanation
- "execute_explain": Task that requires running tools and showing results
- "clarify": Ambiguous request needing more information
- "confirm": Potentially destructive/risky action

RISK LEVELS:
- "low": Reading, writing non-critical files, searches, chat
- "medium": Modifying config files, running scripts
- "high": Deleting files, changing credentials, system commands

CRITICAL: If the user asks to do something the system CAN do (files, search, commands), it is NEVER "chat".
"""


async def classify_intent(user_input: str) -> Dict[str, Any]:
    """Classify the user's intent using local LLM."""
    recent = get_recent_messages(limit=6)
    context_str = "\n".join([f"{m['role']}: {m['content'][:100]}" for m in recent[-6:]]) or "No recent messages"

    prompt = INTENT_PROMPT.format(
        capabilities=SYSTEM_CAPABILITIES,
        user_input=user_input,
        recent_context=context_str
    )

    try:
        async with aiohttp.ClientSession() as session:
            payload = {"model": CONFIG.MODEL_ROUTER, "prompt": prompt, "stream": False}
            async with session.post(OLLAMA_GENERATE_URL, json=payload, timeout=30) as resp:
                if resp.status != 200:
                    return {"intent": "execute_explain", "confidence": 0.5, "risk_level": "low",
                            "reasoning": "LLM unavailable", "task_summary": user_input}

                result = await resp.json()
                response_text = result.get("response", "{}")

                if '<think>' in response_text:
                    response_text = response_text.split('</think>')[-1]
                if '```json' in response_text:
                    response_text = response_text.split('```json')[1].split('```')[0]
                elif '```' in response_text:
                    response_text = response_text.split('```')[1].split('```')[0]

                match = re.search(r'\{[\s\S]*\}', response_text)
                if match:
                    response_text = match.group(0)

                return json.loads(response_text.strip())

    except Exception as e:
        return {"intent": "execute_explain", "confidence": 0.5, "risk_level": "low",
                "reasoning": f"Classification error: {str(e)[:50]}", "task_summary": user_input}


SYNTHESIS_PROMPT = """{capabilities}

Generate a natural conversational response for what just happened.

USER'S ORIGINAL REQUEST: "{original_request}"

EXECUTION RESULTS:
- Intent: {intent}
- Status: {status}
- Summary: {summary}
- Files Created/Modified: {files}
- Content Created: {content_preview}
- Errors (if any): {errors}

RESPONSE GUIDELINES:
- If content was created (poem, code, file listing), INCLUDE IT in your response
- If a file was saved, mention the path
- Be conversational, concise, and warm

CRITICAL RULES:
- NEVER invent or hallucinate data not in the execution results above
- If status is "Failed": Say what went wrong and quote the actual error. NEVER pretend success.
- If status is "Success": Report ONLY what is in the Summary and Content Created fields

Respond with ONLY the message to show the user:
"""

CHAT_PROMPT = """{capabilities}

You are in CHAT mode (no execution happening). The user is just talking.

CONVERSATION HISTORY:
{history}

USER: {user_input}

Respond naturally and conversationally. Keep responses concise.
No asterisk actions like *leans in* or *whispers*.

A:"""

CHAT_INFORMED_PROMPT = """{capabilities}

You are in INFORMED CHAT mode. The user asked a question that benefits from your memory/knowledge.
Below is relevant context retrieved from your memory store.

RETRIEVED CONTEXT:
{rag_context}

CONVERSATION HISTORY:
{history}

USER: {user_input}

Respond naturally, grounded in the retrieved context. Synthesize — don't quote verbatim.

A:"""


async def generate_chat_response(user_input: str) -> str:
    recent = get_recent_messages(limit=10)
    history_str = "\n".join([
        f"{'User' if m['role']=='user' else 'Assistant'}: {m['content']}"
        for m in recent[-10:]
    ]) or "No previous messages"

    prompt = CHAT_PROMPT.format(
        capabilities=SYSTEM_CAPABILITIES,
        history=history_str,
        user_input=user_input
    )

    try:
        async with aiohttp.ClientSession() as session:
            payload = {"model": CONFIG.MODEL_ROUTER, "prompt": prompt, "stream": False}
            async with session.post(OLLAMA_GENERATE_URL, json=payload, timeout=30) as resp:
                if resp.status != 200:
                    return "I'm having trouble thinking right now. Could you try again?"
                result = await resp.json()
                response = result.get("response", "").strip()
                if '<think>' in response:
                    response = response.split('</think>')[-1].strip()
                return response or "Hmm, I'm not sure how to respond to that."
    except Exception as e:
        return f"I encountered an issue: {str(e)[:100]}"


async def generate_informed_chat_response(user_input: str) -> str:
    """Generate a chat response augmented with RAG context."""
    rag_context = "No relevant context found."
    try:
        import requests
        rag_resp = requests.post(
            f"{RAG_BRIDGE_URL}/query",
            json={"query": user_input, "k": 5},
            timeout=3
        )
        if rag_resp.status_code == 200:
            results = rag_resp.json().get("results", [])
            if results:
                context_parts = []
                for r in results:
                    text = r.get("text", "")
                    source = r.get("source", "unknown")
                    if isinstance(source, str):
                        source = source.replace("\\", "/").split("/")[-1]
                    desc = r.get("description", "")
                    context_parts.append(f"[{desc or source}]: {text}")
                rag_context = "\n\n".join(context_parts)
    except Exception:
        pass

    recent = get_recent_messages(limit=10)
    history_str = "\n".join([
        f"{'User' if m['role']=='user' else 'Assistant'}: {m['content']}"
        for m in recent[-10:]
    ]) or "No previous messages"

    prompt = CHAT_INFORMED_PROMPT.format(
        capabilities=SYSTEM_CAPABILITIES,
        rag_context=rag_context,
        history=history_str,
        user_input=user_input
    )

    try:
        async with aiohttp.ClientSession() as session:
            payload = {"model": CONFIG.MODEL_ROUTER, "prompt": prompt, "stream": False}
            async with session.post(OLLAMA_GENERATE_URL, json=payload, timeout=30) as resp:
                if resp.status != 200:
                    return "I'm having trouble thinking right now. Could you try again?"
                result = await resp.json()
                response = result.get("response", "").strip()
                if '<think>' in response:
                    response = response.split('</think>')[-1].strip()
                return response or "Hmm, I'm not sure how to respond to that."
    except Exception as e:
        return f"I encountered an issue: {str(e)[:100]}"


async def synthesize_response(
    original_request: str,
    intent: str,
    success: bool,
    summary: str = "",
    files: List[str] = None,
    content_preview: str = "",
    errors: str = ""
) -> str:
    prompt = SYNTHESIS_PROMPT.format(
        capabilities=SYSTEM_CAPABILITIES,
        original_request=original_request,
        intent=intent,
        status="Success" if success else "Failed",
        summary=summary or "Task completed",
        files=", ".join(files) if files else "None",
        content_preview=content_preview[:500] if content_preview else "None",
        errors=errors or "None"
    )

    try:
        async with aiohttp.ClientSession() as session:
            payload = {"model": CONFIG.MODEL_ROUTER, "prompt": prompt, "stream": False}
            async with session.post(OLLAMA_GENERATE_URL, json=payload, timeout=30) as resp:
                if resp.status != 200:
                    return f"Done! {summary}" if success else f"Something went wrong: {errors or summary}"
                result = await resp.json()
                response = result.get("response", "").strip()
                if '<think>' in response:
                    response = response.split('</think>')[-1].strip()
                return response or ("Task completed!" if success else "Something went wrong.")
    except Exception as e:
        return f"Done! {summary}" if success else f"Error: {str(e)[:100]}"


def format_confirmation_request(action: str, details: str = None) -> str:
    return f"""⚠️ **Confirmation Required**

You asked me to: *{action}*

{details or "This operation may have significant effects."}

**Reply 'yes' to confirm or 'no' to cancel.**"""


def format_clarification_request(action: str, question: str = None) -> str:
    if question:
        return f"🤔 {question}"
    return f"🤔 I want to help with '{action}', but I need a bit more detail. Could you be more specific?"
