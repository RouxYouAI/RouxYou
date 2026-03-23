import json
import os
import re
import time
from typing import List, Dict, Optional
from pathlib import Path
from collections import defaultdict
from .schemas import EpisodicMemory, SkillRecord, TaskContext
from .redact import redact as _shared_redact

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from config import CONFIG

# === FILE LOCKING ===
LOCK_TIMEOUT = 5

def _acquire_lock(lock_path: Path, timeout: float = LOCK_TIMEOUT):
    lock_file = open(lock_path, 'w')
    start = time.time()
    while True:
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return lock_file
        except (IOError, OSError, BlockingIOError):
            if time.time() - start > timeout:
                lock_file.close()
                return None
            time.sleep(0.1)

def _release_lock(lock_file):
    if lock_file:
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock_file, fcntl.LOCK_UN)
            lock_file.close()
        except Exception:
            try:
                lock_file.close()
            except Exception:
                pass

BASE_DIR = Path(os.path.dirname(os.path.abspath(__file__))).parent
MEMORY_FILE = BASE_DIR / "memory.json"
MEMORY_LOCK = BASE_DIR / "memory.json.lock"

# Decay configuration from config.yaml
AGE_HALF_LIFE_DAYS = CONFIG.MEMORY_HALF_LIFE
MIN_UTILITY = CONFIG.MEMORY_MIN_UTIL
MAX_AGE_DAYS = CONFIG.MEMORY_MAX_AGE


class MemorySystem:
    def __init__(self):
        self.memories: List[EpisodicMemory] = []
        self._load_memory()

    def _load_memory(self):
        if MEMORY_FILE.exists():
            try:
                with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.memories = [EpisodicMemory(**m) for m in data]
                print(f"MEMORY: Loaded {len(self.memories)} episodes.")
            except Exception as e:
                print(f"MEMORY WARNING: Corrupt database, starting fresh. ({e})")
                self.memories = []
        else:
            print("MEMORY: No existing memory found. Starting fresh.")

    def _redact(self, text: str) -> str:
        return _shared_redact(text)

    def save_episode(self, task: str, plan_summary: str, context: TaskContext, success: bool,
                     plan_steps: list = None, execution_results: list = None):
        clean_summary = self._redact(plan_summary)
        code_artifacts = {}
        if plan_steps and execution_results:
            for step, result in zip(plan_steps, execution_results):
                step_data = step if isinstance(step, dict) else {}
                result_data = result.get("result", {}) if isinstance(result, dict) else {}
                if step_data.get("action") in ("write_file", "patch_file"):
                    fname = step_data.get("details", "unknown")
                    code = step_data.get("content", "")
                    if code:
                        code_artifacts[fname] = self._redact(code)
                if step_data.get("action") == "run_command" and result_data.get("success"):
                    cmd = step_data.get("details", "")
                    output = result_data.get("output", "")[:500]
                    if output:
                        code_artifacts[f"cmd:{cmd[:80]}"] = self._redact(output)
        clean_plan = None
        if plan_steps:
            clean_plan = []
            for s in plan_steps:
                cs = dict(s) if isinstance(s, dict) else {}
                if "content" in cs:
                    cs["content"] = self._redact(cs.get("content", ""))
                if "details" in cs:
                    cs["details"] = self._redact(cs.get("details", ""))
                clean_plan.append(cs)
        clean_results = None
        if execution_results:
            clean_results = []
            for r in execution_results:
                cr = dict(r) if isinstance(r, dict) else {}
                res = cr.get("result", {})
                if isinstance(res, dict):
                    for key in ("content", "output"):
                        if key in res:
                            res[key] = self._redact(str(res[key]))[:1000]
                    cr["result"] = res
                clean_results.append(cr)
        utility = self._calculate_utility(success, execution_results)
        episode = EpisodicMemory(
            timestamp=time.time(),
            task_query=task,
            plan_summary=clean_summary,
            working_dir=context.working_dir,
            affected_files=[context.active_file] if context.active_file else [],
            success=success,
            plan_steps=clean_plan,
            execution_results=clean_results,
            code_artifacts=code_artifacts if code_artifacts else None,
            utility=utility,
        )
        self.memories.append(episode)
        self._persist()
        artifact_count = len(code_artifacts) if code_artifacts else 0
        print(f"MEMORY: Saved episode '{task[:30]}...' (Success: {success}, Utility: {utility:.2f}, Artifacts: {artifact_count})")

    def _calculate_utility(self, success: bool, execution_results: list = None) -> float:
        if not success:
            return 0.1
        if not execution_results:
            return 0.7
        total_steps = len(execution_results)
        successful_steps = sum(
            1 for r in execution_results
            if isinstance(r, dict) and r.get("result", {}).get("success", False)
        )
        step_ratio = successful_steps / max(total_steps, 1)
        has_code = any(
            isinstance(r, dict) and r.get("action") in ("write_file", "patch_file")
            for r in execution_results
        )
        code_bonus = 0.1 if has_code else 0.0
        return min(1.0, 0.5 + (step_ratio * 0.4) + code_bonus)

    def _persist(self):
        lock = _acquire_lock(MEMORY_LOCK)
        try:
            with open(MEMORY_FILE, "w", encoding="utf-8") as f:
                json.dump([m.dict() for m in self.memories], f, indent=2)
        except Exception as e:
            print(f"MEMORY ERROR: Could not save to disk: {e}")
        finally:
            _release_lock(lock)

    def decay_utilities(self):
        now = time.time()
        updated = 0
        for mem in self.memories:
            age_days = (now - mem.timestamp) / 86400
            decay_factor = 0.5 ** (age_days / AGE_HALF_LIFE_DAYS)
            if mem.reuse_count > 0 and mem.reuse_successes > 0:
                shield = min(0.3, 0.1 * mem.reuse_successes)
                decay_factor = min(1.0, decay_factor + shield)
            if mem.code_artifacts:
                decay_factor = min(1.0, decay_factor + 0.1)
            new_utility = mem.utility * decay_factor
            if abs(new_utility - mem.utility) > 0.001:
                mem.utility = round(new_utility, 4)
                updated += 1
        if updated:
            print(f"MEMORY: Decayed utilities on {updated}/{len(self.memories)} episodes")
            self._persist()

    def run_decay(self) -> Dict:
        now = time.time()
        initial_count = len(self.memories)
        self.decay_utilities()
        groups = defaultdict(list)
        for i, mem in enumerate(self.memories):
            query = re.sub(r'^TASK:\s*', '', mem.task_query, flags=re.IGNORECASE)
            context_idx = query.find('\nCONTEXT:')
            if context_idx >= 0:
                query = query[:context_idx]
            query = ' '.join(query.lower().split())
            if len(query) >= 5:
                groups[query].append(i)
        dup_indices = set()
        for query, indices in groups.items():
            if len(indices) > 1:
                best = max(indices, key=lambda i: self.memories[i].utility)
                for idx in indices:
                    if idx != best:
                        dup_indices.add(idx)
        prune_indices = set()
        for i, mem in enumerate(self.memories):
            age_days = (now - mem.timestamp) / 86400
            if age_days > MAX_AGE_DAYS:
                prune_indices.add(i)
            elif mem.utility < MIN_UTILITY:
                prune_indices.add(i)
        remove = dup_indices | prune_indices
        if remove:
            self.memories = [m for i, m in enumerate(self.memories) if i not in remove]
            self._persist()
        stats = {
            "initial": initial_count,
            "duplicates_removed": len(dup_indices),
            "expired_pruned": len(prune_indices - dup_indices),
            "remaining": len(self.memories),
            "total_removed": len(remove),
        }
        print(f"MEMORY DECAY: {initial_count} -> {len(self.memories)} episodes "
              f"(-{len(dup_indices)} dups, -{len(prune_indices - dup_indices)} expired)")
        return stats

    STOPWORDS = {
        "the", "a", "an", "in", "on", "at", "to", "for", "of", "and", "or", "is", "it",
        "my", "me", "i", "you", "can", "do", "please", "what", "how", "where", "when",
        "this", "that", "with", "from", "be", "are", "was", "were", "will", "would",
        "could", "should", "have", "has", "had", "not", "but", "if", "then", "so",
        "just", "about", "up", "out", "all", "some", "any", "there", "here",
        "task", "context", "let", "try", "tell", "show", "get", "make", "take",
        "know", "want", "need", "use", "go", "see", "look", "find", "give",
    }

    def _extract_keywords(self, text: str) -> set:
        words = set(text.lower().split())
        return {w for w in words if w not in self.STOPWORDS and len(w) > 2}

    def retrieve_relevant(self, query: str, limit: int = 3, min_score: float = 3.0) -> List[EpisodicMemory]:
        query_keywords = self._extract_keywords(query)
        if not query_keywords:
            return []
        scored = []
        for mem in self.memories:
            score = 0.0
            task_keywords = self._extract_keywords(mem.task_query)
            keyword_overlap = query_keywords & task_keywords
            score += len(keyword_overlap) * 2.0
            files_text = " ".join(mem.affected_files).lower()
            for kw in query_keywords:
                if kw in files_text:
                    score += 1.5
            summary_lower = mem.plan_summary.lower()
            for kw in query_keywords:
                if kw in summary_lower:
                    score += 0.5
            if mem.code_artifacts:
                artifacts_text = " ".join(mem.code_artifacts.values()).lower()
                for kw in query_keywords:
                    if kw in artifacts_text:
                        score += 1.0
            query_lower = query.lower()
            task_lower = mem.task_query.lower()
            query_words = query_lower.split()
            for i in range(len(query_words) - 1):
                bigram = f"{query_words[i]} {query_words[i+1]}"
                if bigram in task_lower and bigram not in ("the ", " the"):
                    score += 3.0
            if score > 0:
                scored.append((score, mem))
        if not scored:
            return []
        reranked = []
        for sim_score, mem in scored:
            utility = getattr(mem, 'utility', 0.5)
            if not mem.success:
                utility *= 0.3
            artifact_bonus = 0.1 if mem.code_artifacts else 0.0
            reuse_bonus = 0.0
            reuse_count = getattr(mem, 'reuse_count', 0)
            reuse_successes = getattr(mem, 'reuse_successes', 0)
            if reuse_count > 0:
                reuse_bonus = 0.1 * (reuse_successes / reuse_count)
            effective_utility = min(1.0, utility + artifact_bonus + reuse_bonus)
            final_score = sim_score * (0.6 + 0.4 * effective_utility)
            reranked.append((final_score, sim_score, mem))
        reranked.sort(key=lambda x: x[0], reverse=True)
        results = [mem for fs, _, mem in reranked[:limit] if fs >= min_score]
        return results

    def record_reuse(self, episode: EpisodicMemory, success: bool):
        episode.reuse_count += 1
        if success:
            episode.reuse_successes += 1
        if episode.reuse_count >= 2:
            reuse_rate = episode.reuse_successes / episode.reuse_count
            episode.utility = 0.6 * episode.utility + 0.4 * reuse_rate
        self._persist()


memory = MemorySystem()
