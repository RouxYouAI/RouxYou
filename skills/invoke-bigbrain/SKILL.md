---
name: invoke-bigbrain
description: When and how to escalate to bigbrain (Claude Opus 4.8 cloud API). Use when local v3 is uncertain, when a proposal is governance-relevant, or when post-application verification is needed.
---

# Invoke Bigbrain

## When to use

Bigbrain = Claude Opus 4.8 via cloud API. NOT the default. Use only when:

1. **Governance-relevant proposal.** Modifying Roux's own runtime (companion.py, proposal_executor.py, shadow, resolver). Disclosure rule v1.3 routes these around Roux's self-shadow → bigbrain.
2. **Coder pre-execution review.** Pillar 3 `_handle_code` calls `bigbrain_review_planned_action(kind="code-plan")` before worker executes.
3. **Coder post-application verification.** Same pipeline, `kind="code-implementation"`, after worker writes files.
4. **Coaching channel writeback.** Bigbrain FAIL on a /propose → COACHING field → `claude-memory` era `roux_coaching`.
5. **Local v3 + authoring both fail.** 3-tier fallback chain: authoring → companion → bigbrain.

## Cost / latency awareness

- Opus 4.8 is expensive. Local first, always.
- Strips deprecated `temperature` param from request (4.7 doesn't accept it).
- Retries 4x on 529 Overloaded with backoff `[500, 2000, 5000, 12000]` ms.
- Typical latency: 5-30s depending on context size.

## How to invoke

```python
from shared.llm import llm_chat
response = llm_chat(
    alias_or_model="bigbrain",  # resolves to claude-opus-4-8
    messages=[...],
    system="...",
    max_tokens=4096,
)
```

Or via the higher-level helpers:
- `bigbrain_review_planned_action(kind=..., context=...)` — typed review entrypoint
- `claude_memory.record_to_era("roux_coaching", ...)` — coaching writeback

## What NOT to do

- Don't call bigbrain for chat. Local v3 handles voice.
- Don't call bigbrain for routine /propose drafts. Authoring LoRA handles that.
- Don't call bigbrain to "double-check" a passing local result. Asymmetric trust: PASS doesn't need escalation.

## Files involved

- `shared/llm.py::ClaudeProvider` — request handling, retry, dep-param strip
- `shared/bigbrain_review.py` — typed review entrypoints
- `shared/disclosure_rule.py` — governance-relevance routing
