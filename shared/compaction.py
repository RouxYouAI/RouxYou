"""
RouxYou — Context Compaction & Token Budget Management (Phase 35)
=================================================================
Ensures Coder's prompt stays within the model's context window.
Estimates token usage per section, compacts when needed.

Usage:
    from shared.compaction import PromptBudget, compact_prompt_sections

    budget = PromptBudget(model_context=32768)
    sections = {
        "system": system_prompt_text,
        "memories": memory_text,
        "skills": skill_text,
        "system_map": system_map_text,
        "query": user_query,
    }
    compacted = budget.fit(sections)
    # compacted["memories"] may be trimmed
    # compacted["skills"] may be trimmed
    # compacted["_report"] has the budget breakdown
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("roux.compaction")


# ============================================================
# Token Estimation
# ============================================================

def estimate_tokens(text: str) -> int:
    """
    Heuristic token count. ~4 chars per token for English/code mix.
    Conservative (slightly overestimates) to avoid overflow.

    Claw-code uses chars/3.5; we use chars/3.8 for code-heavy prompts
    since code tokens are denser than natural language.
    """
    if not text:
        return 0
    return max(1, len(text) // 4)


def estimate_tokens_precise(text: str) -> int:
    """
    Slightly more accurate estimation that accounts for whitespace,
    code blocks, and JSON structure.
    """
    if not text:
        return 0

    # Count code blocks separately (code is ~3.2 chars/token)
    code_chars = 0
    prose_chars = 0
    in_code = False

    for line in text.split("\n"):
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            code_chars += len(line) + 1
        else:
            prose_chars += len(line) + 1

    code_tokens = code_chars / 3.2 if code_chars else 0
    prose_tokens = prose_chars / 4.0 if prose_chars else 0

    return max(1, int(code_tokens + prose_tokens))


# ============================================================
# Prompt Budget Manager
# ============================================================

# Priority order for compaction (lowest priority compacted first)
COMPACTION_PRIORITY = [
    "skills",       # Compact first — helpful but not critical
    "memories",     # Compact second — important but can be trimmed
    "system_map",   # Compact third — useful context, can be summarized
    # "system" and "query" are never compacted
]


@dataclass
class BudgetReport:
    """Report on token budget allocation and any compaction performed."""
    model_context: int = 32768
    output_reserve: int = 4096
    input_ceiling: float = 0.0  # computed
    total_before: int = 0
    total_after: int = 0
    sections_before: dict = field(default_factory=dict)
    sections_after: dict = field(default_factory=dict)
    compacted: list = field(default_factory=list)  # which sections were trimmed
    under_budget: bool = True

    def __post_init__(self):
        self.input_ceiling = self.model_context - self.output_reserve

    def __str__(self):
        status = "OK" if self.under_budget else "OVER BUDGET"
        pct = (self.total_after / self.input_ceiling * 100) if self.input_ceiling else 0
        parts = [f"[Budget {status}: {self.total_after}/{int(self.input_ceiling)} tokens ({pct:.0f}%)]"]
        if self.compacted:
            parts.append(f"  Compacted: {', '.join(self.compacted)}")
        for name, tokens in self.sections_after.items():
            marker = " *" if name in self.compacted else ""
            parts.append(f"  {name}: {tokens} tokens{marker}")
        return "\n".join(parts)


class PromptBudget:
    """
    Manages token budget for a model's context window.

    Usage:
        budget = PromptBudget(model_context=32768)
        result = budget.fit(sections)
        # result contains the (possibly compacted) sections
        # result["_report"] is a BudgetReport
    """

    def __init__(self, model_context: int = 32768, output_reserve: int = 4096,
                 budget_fraction: float = 0.70):
        self.model_context = model_context
        self.output_reserve = output_reserve
        # Input ceiling: leave room for output + safety margin
        self.input_ceiling = int((model_context - output_reserve) * budget_fraction)

    def fit(self, sections: dict[str, str]) -> dict[str, str]:
        """
        Fit prompt sections within the token budget.
        Returns a dict with the same keys (possibly compacted values)
        plus a "_report" key with the BudgetReport.

        Sections are compacted in COMPACTION_PRIORITY order.
        "system" and "query" sections are never touched.
        """
        report = BudgetReport(
            model_context=self.model_context,
            output_reserve=self.output_reserve,
        )
        report.input_ceiling = self.input_ceiling

        # Estimate tokens per section
        section_tokens = {}
        for name, text in sections.items():
            tokens = estimate_tokens(text)
            section_tokens[name] = tokens
            report.sections_before[name] = tokens

        report.total_before = sum(section_tokens.values())

        # Check if we're under budget
        if report.total_before <= self.input_ceiling:
            report.total_after = report.total_before
            report.sections_after = dict(section_tokens)
            report.under_budget = True
            result = dict(sections)
            result["_report"] = report
            return result

        # Over budget — need to compact
        logger.warning(
            f"Prompt over budget: {report.total_before} tokens "
            f"> {self.input_ceiling} ceiling. Compacting..."
        )

        result = dict(sections)
        current_total = report.total_before

        for section_name in COMPACTION_PRIORITY:
            if section_name not in result:
                continue
            if current_total <= self.input_ceiling:
                break

            overshoot = current_total - self.input_ceiling
            section_text = result[section_name]
            section_tok = section_tokens[section_name]

            if section_tok <= 0:
                continue

            # How much do we need to cut from this section?
            if section_name == "memories":
                result[section_name] = _compact_memories(section_text, overshoot)
            elif section_name == "skills":
                result[section_name] = _compact_skills(section_text, overshoot)
            elif section_name == "system_map":
                result[section_name] = _compact_system_map(section_text, overshoot)
            else:
                # Generic: truncate with notice
                result[section_name] = _generic_truncate(section_text, overshoot)

            new_tokens = estimate_tokens(result[section_name])
            saved = section_tok - new_tokens
            current_total -= saved
            section_tokens[section_name] = new_tokens
            report.compacted.append(section_name)
            logger.info(f"Compacted {section_name}: {section_tok} -> {new_tokens} tokens (saved {saved})")

        report.total_after = current_total
        report.sections_after = dict(section_tokens)
        report.under_budget = current_total <= self.input_ceiling

        if not report.under_budget:
            logger.error(
                f"Still over budget after compaction: {current_total} > {self.input_ceiling}. "
                f"Consider reducing memory retrieval count or skill limit."
            )

        result["_report"] = report
        return result


# ============================================================
# Section-specific compaction strategies
# ============================================================

def _compact_memories(text: str, tokens_to_save: int) -> str:
    """
    Compact memory section by:
    1. Truncating code artifacts (biggest savings)
    2. Reducing number of memories shown
    3. Shortening plan summaries
    """
    if not text:
        return text

    lines = text.split("\n")
    # Find memory boundaries (each starts with "- Past Task:")
    memory_blocks = []
    current_block = []

    for line in lines:
        if line.startswith("- Past Task:") and current_block:
            memory_blocks.append("\n".join(current_block))
            current_block = [line]
        else:
            current_block.append(line)
    if current_block:
        memory_blocks.append("\n".join(current_block))

    # Strategy 1: Remove code artifacts from all memories
    compacted_blocks = []
    for block in memory_blocks:
        # Strip code blocks
        compact = []
        in_code = False
        for line in block.split("\n"):
            if "```" in line:
                in_code = not in_code
                if not in_code:
                    compact.append("    [code artifact trimmed for context budget]")
                continue
            if not in_code:
                compact.append(line)
        compacted_blocks.append("\n".join(compact))

    result = "\n".join(compacted_blocks)
    if estimate_tokens(text) - estimate_tokens(result) >= tokens_to_save:
        return result

    # Strategy 2: Drop the least relevant memory (last one)
    if len(compacted_blocks) > 1:
        dropped = compacted_blocks[:-1]
        result = "\n".join(dropped)
        result += "\n[1 additional memory trimmed for context budget]"

    return result


def _compact_skills(text: str, tokens_to_save: int) -> str:
    """
    Compact skills by:
    1. Truncating code patterns
    2. Reducing number of skills
    """
    if not text:
        return text

    lines = text.split("\n")
    # Find skill boundaries (each starts with "### Skill:")
    skill_blocks = []
    header_lines = []
    current_block = []

    for line in lines:
        if line.startswith("### Skill:") and current_block:
            skill_blocks.append("\n".join(current_block))
            current_block = [line]
        elif not skill_blocks and not line.startswith("### Skill:"):
            header_lines.append(line)
        else:
            current_block.append(line)
    if current_block:
        skill_blocks.append("\n".join(current_block))

    # Strategy 1: Truncate code patterns to 200 chars
    compacted = []
    for block in skill_blocks:
        compact_lines = []
        in_code = False
        code_chars = 0
        for line in block.split("\n"):
            if "```" in line:
                in_code = not in_code
                compact_lines.append(line)
                if not in_code and code_chars > 200:
                    # Already truncated
                    pass
                continue
            if in_code:
                code_chars += len(line)
                if code_chars <= 200:
                    compact_lines.append(line)
                elif code_chars - len(line) <= 200:
                    compact_lines.append("    # ... [truncated for context budget]")
            else:
                compact_lines.append(line)
        compacted.append("\n".join(compact_lines))

    result = "\n".join(header_lines) + "\n" + "\n".join(compacted)
    if estimate_tokens(text) - estimate_tokens(result) >= tokens_to_save:
        return result

    # Strategy 2: Keep only first 2 skills
    if len(compacted) > 2:
        result = "\n".join(header_lines) + "\n" + "\n".join(compacted[:2])
        result += f"\n[{len(compacted) - 2} additional skill(s) trimmed for context budget]"

    return result


def _compact_system_map(text: str, tokens_to_save: int) -> str:
    """Truncate the system architecture map, keeping the most essential modules."""
    if not text:
        return text

    lines = text.split("\n")
    # Keep ~60% of lines
    keep = max(5, int(len(lines) * 0.6))
    result = "\n".join(lines[:keep])
    result += f"\n[System map truncated: showing {keep}/{len(lines)} entries for context budget]"
    return result


def _generic_truncate(text: str, tokens_to_save: int) -> str:
    """Last-resort truncation for any section."""
    target_tokens = estimate_tokens(text) - tokens_to_save
    if target_tokens <= 0:
        return "[Section removed for context budget]"

    # Approximate character count for target tokens
    target_chars = target_tokens * 4
    return text[:target_chars] + "\n[... truncated for context budget]"


# ============================================================
# Convenience function for Coder integration
# ============================================================

def compact_coder_prompt(
    system_prompt: str,
    memory_text: str,
    skill_text: str,
    system_map: str,
    user_query: str,
    model_context: int = 32768,
    output_reserve: int = 4096,
) -> dict[str, str]:
    """
    Convenience wrapper for Coder's prompt construction.
    Returns compacted sections ready for prompt assembly.

    Usage in coder.py:
        from shared.compaction import compact_coder_prompt

        sections = compact_coder_prompt(
            system_prompt=base_prompt,
            memory_text=memory_text,
            skill_text=skill_text,
            system_map=system_map,
            user_query=query_text,
        )
        # Use sections["system"], sections["memories"], etc.
        # Check sections["_report"] for budget details
    """
    budget = PromptBudget(
        model_context=model_context,
        output_reserve=output_reserve,
    )

    sections = {
        "system": system_prompt,
        "memories": memory_text,
        "skills": skill_text,
        "system_map": system_map,
        "query": user_query,
    }

    result = budget.fit(sections)

    report = result["_report"]
    pct = (report.total_after / report.input_ceiling * 100) if report.input_ceiling else 0
    logger.info(
        f"Prompt budget: {report.total_after}/{int(report.input_ceiling)} tokens "
        f"({pct:.0f}%) | Compacted: {report.compacted or 'none'}"
    )

    return result
