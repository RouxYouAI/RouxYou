"""
Skill Extractor — Learns reusable patterns from successful task episodes.
Skills are stored in skills.json and injected into the Coder's system prompt
to help it solve similar problems faster.
"""

import json
import time
from pathlib import Path
from typing import List, Dict, Any, Optional

from .schemas import SkillRecord

BASE_DIR = Path(__file__).parent.parent
SKILLS_FILE = BASE_DIR / "skills.json"


def _load_skills() -> List[Dict[str, Any]]:
    """Load skills from disk."""
    if not SKILLS_FILE.exists():
        return []
    try:
        with open(SKILLS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, Exception):
        return []


def _save_skills(skills: List[Dict[str, Any]]):
    """Persist skills to disk."""
    with open(SKILLS_FILE, "w", encoding="utf-8") as f:
        json.dump(skills, f, indent=2)


def get_skills_for_task(query: str, limit: int = 3) -> List[Dict[str, Any]]:
    """
    Find skills relevant to a task query using keyword matching.
    Returns up to `limit` skills sorted by relevance score.
    """
    skills = _load_skills()
    if not skills:
        return []

    query_lower = query.lower()
    query_words = set(query_lower.split())

    scored = []
    for skill in skills:
        score = 0.0

        # Name match (strongest signal)
        name_lower = skill.get("name", "").lower()
        name_words = set(name_lower.replace("_", " ").replace("-", " ").split())
        name_overlap = query_words & name_words
        score += len(name_overlap) * 3.0

        # Description match
        desc_lower = skill.get("description", "").lower()
        for word in query_words:
            if len(word) > 3 and word in desc_lower:
                score += 1.0

        # Code pattern match (if the query mentions specific functions/patterns)
        pattern_lower = skill.get("code_pattern", "").lower()
        for word in query_words:
            if len(word) > 3 and word in pattern_lower:
                score += 1.5

        # Dependency match (if query mentions a library/file)
        deps = [d.lower() for d in skill.get("dependencies", [])]
        for word in query_words:
            if word in deps:
                score += 2.0

        # Success rate bonus
        times_used = skill.get("times_used", 0)
        times_succeeded = skill.get("times_succeeded", 0)
        if times_used >= 2:
            success_rate = times_succeeded / times_used
            score *= (0.5 + 0.5 * success_rate)  # Penalize low-success skills

        if score > 0:
            scored.append((score, skill))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [skill for _, skill in scored[:limit]]


def format_skills_for_prompt(skills: List[Dict[str, Any]]) -> str:
    """
    Format skills into a section for the Coder's system prompt.
    """
    if not skills:
        return ""

    lines = ["## RELEVANT SKILLS (PROVEN PATTERNS)"]
    for skill in skills:
        name = skill.get("name", "unnamed")
        desc = skill.get("description", "")
        pattern = skill.get("code_pattern", "")
        deps = skill.get("dependencies", [])
        used = skill.get("times_used", 0)
        succeeded = skill.get("times_succeeded", 0)

        lines.append(f"\n### Skill: {name}")
        if desc:
            lines.append(f"  Purpose: {desc}")
        if deps:
            lines.append(f"  Dependencies: {', '.join(deps)}")
        if used > 0:
            rate = (succeeded / used * 100) if used else 0
            lines.append(f"  Track record: {succeeded}/{used} successful ({rate:.0f}%)")
        if pattern:
            # Truncate very long patterns
            preview = pattern[:600]
            if len(pattern) > 600:
                preview += "\n  ... (truncated)"
            lines.append(f"  Code pattern:\n```\n{preview}\n```")

    lines.append("\nAdapt these patterns where applicable instead of writing from scratch.")
    return "\n".join(lines)


def record_skill_usage(skill_name: str, success: bool):
    """Record that a skill was used (successfully or not)."""
    skills = _load_skills()
    for skill in skills:
        if skill.get("name") == skill_name:
            skill["times_used"] = skill.get("times_used", 0) + 1
            if success:
                skill["times_succeeded"] = skill.get("times_succeeded", 0) + 1
            _save_skills(skills)
            return True
    return False


def extract_skill_from_episode(
    task_query: str,
    plan_summary: str,
    code_artifacts: Optional[Dict[str, str]] = None,
    affected_files: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Extract a reusable skill from a successful task episode.
    Returns a skill dict if the episode looks reusable, None otherwise.
    """
    if not code_artifacts:
        return None

    # Only extract skills from episodes with actual code changes
    code_files = {k: v for k, v in code_artifacts.items() if not k.startswith("cmd:")}
    if not code_files:
        return None

    # Build skill name from the task query (simplified)
    name_words = []
    stopwords = {"the", "a", "an", "to", "in", "on", "for", "and", "or", "is", "it",
                 "fix", "add", "update", "create", "make", "please", "can", "you"}
    for word in task_query.lower().split():
        clean = word.strip(".,!?:;\"'()[]")
        if clean and clean not in stopwords and len(clean) > 2:
            name_words.append(clean)
    if not name_words:
        return None

    skill_name = "_".join(name_words[:4])

    # Check for duplicates
    existing = _load_skills()
    for s in existing:
        if s.get("name") == skill_name:
            return None  # Already exists

    # Pick the most substantial code artifact as the pattern
    best_file = max(code_files.items(), key=lambda x: len(x[1]))
    code_pattern = best_file[1][:1000]  # Cap at 1000 chars

    # Infer dependencies from file paths
    dependencies = []
    if affected_files:
        for f in affected_files:
            name = Path(f).name
            if name:
                dependencies.append(name)

    return {
        "name": skill_name,
        "description": plan_summary[:200],
        "code_pattern": code_pattern,
        "dependencies": dependencies,
        "times_used": 1,
        "times_succeeded": 1,
        "created_at": time.time(),
    }


def add_skill(skill: Dict[str, Any]) -> bool:
    """Add a new skill to the skills database."""
    skills = _load_skills()

    # Dedup by name
    for existing in skills:
        if existing.get("name") == skill.get("name"):
            return False

    skills.append(skill)
    _save_skills(skills)
    return True


def get_all_skills() -> List[Dict[str, Any]]:
    """Return all skills."""
    return _load_skills()


def remove_skill(skill_name: str) -> bool:
    """Remove a skill by name."""
    skills = _load_skills()
    original_count = len(skills)
    skills = [s for s in skills if s.get("name") != skill_name]
    if len(skills) < original_count:
        _save_skills(skills)
        return True
    return False
