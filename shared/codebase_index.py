"""
Codebase Index — structural map of the RouxYou source tree.

Builds a live AST-based index of all core Python modules so agents
have architectural self-awareness: WHERE things are, WHAT they expose,
and HOW they connect — not just what happened historically (that's RAG).

Inspired by PageIndex's tree-search approach but lightweight and local.
Pre-built at startup, injected into the Coder's context window.
"""

import ast
import os
import time
import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_HERE     = Path(__file__).parent          # shared/
BASE_DIR  = _HERE.parent                    # project root

# Core modules to index — paths relative to project root
CORE_MODULES = {
    "orchestrator":          BASE_DIR / "orchestrator" / "orchestrator.py",
    "coder":                 BASE_DIR / "coder"        / "coder.py",
    "worker":                BASE_DIR / "worker"       / "worker.py",
    "gateway":               BASE_DIR / "gateway"      / "gateway.py",
    "dashboard":             BASE_DIR / "dashboard.py",
    "shared/schemas":        BASE_DIR / "shared" / "schemas.py",
    "shared/memory":         BASE_DIR / "shared" / "memory.py",
    "shared/companion":      BASE_DIR / "shared" / "companion.py",
    "shared/activity":       BASE_DIR / "shared" / "activity.py",
    "shared/logger":         BASE_DIR / "shared" / "logger.py",
    "shared/lifecycle":      BASE_DIR / "shared" / "lifecycle.py",
    "shared/task_queue":     BASE_DIR / "shared" / "task_queue.py",
    "shared/task_registry":  BASE_DIR / "shared" / "task_registry.py",
    "shared/conversations":  BASE_DIR / "shared" / "conversations.py",
    "shared/deployer":       BASE_DIR / "shared" / "deployer.py",
    "shared/proposer":       BASE_DIR / "shared" / "proposer.py",
    "shared/researcher":     BASE_DIR / "shared" / "researcher.py",
    "shared/search":         BASE_DIR / "shared" / "search.py",
    "services/roux":         BASE_DIR / "services" / "roux" / "roux_service.py",
    "services/watchtower":   BASE_DIR / "services" / "watchtower" / "api.py",
    "memory/memory_agent":   BASE_DIR / "memory" / "memory_agent.py",
    "memory/http_api":       BASE_DIR / "memory" / "http_api.py",
}

INDEX_CACHE = BASE_DIR / "state" / "codebase_index.json"

# 2026-06-18: the hardcoded CORE_MODULES list (above) had drifted — it indexed only ~22
# modules and MISSED the entire self-modification gate (coach, shadow_verifier,
# proposal_executor, sampling_confirmer, verify_toolbelt, reflection_coach). So Roux planned
# edits from a self-map that didn't include the systems she runs on. Now we AUTO-DISCOVER the
# source tree so her self-map always includes her current code, new files included.
_INDEX_DIRS = ["orchestrator", "coder", "worker", "gateway", "shared", "services", "memory"]
_INDEX_EXTRA = ["dashboard.py"]
_INDEX_SKIP = ("__pycache__", "/venv", "node_modules", "/.git", "/migrations/", "/tests/")

# 2026-06-20: extend the self-map to the gen-2 React cockpit (dashboard-next/). It's TS/TSX so
# Python's `ast` can't parse it — TSFileIndex (below) does a lightweight regex scan producing the
# SAME node dict, so the cockpit's own modules appear in her 3D self-map (the dashboard renders
# itself inside her anatomy). The old dashboard.py is legacy/being-sunset.
_TS_ROOT = BASE_DIR / "dashboard-next"
_TS_DIRS = ["app", "lib"]
_TS_EXTS = (".ts", ".tsx")
_TS_SKIP = ("node_modules", "/.next", "/dist/")

def _discover_modules() -> Dict[str, Path]:
    """Glob the core source tree for indexable .py modules (replaces the stale hardcoded list)."""
    mods: Dict[str, Path] = {}
    for d in _INDEX_DIRS:
        base = BASE_DIR / d
        if not base.exists():
            continue
        for p in sorted(base.rglob("*.py")):
            s = str(p)
            if any(x in s for x in _INDEX_SKIP) or p.name.startswith("test_") or p.name == "__init__.py":
                continue
            mods[str(p.relative_to(BASE_DIR)).replace(".py", "")] = p
    for f in _INDEX_EXTRA:
        p = BASE_DIR / f
        if p.exists():
            mods[f.replace(".py", "")] = p
    return mods


def _discover_ts_modules() -> Dict[str, Path]:
    """Glob the gen-2 React dashboard (dashboard-next/) for .ts/.tsx modules — her self-map sees the cockpit."""
    mods: Dict[str, Path] = {}
    if not _TS_ROOT.exists():
        return mods
    for d in _TS_DIRS:
        base = _TS_ROOT / d
        if not base.exists():
            continue
        for p in sorted(base.rglob("*.ts*")):
            s = str(p)
            if not s.endswith(_TS_EXTS) or p.name.endswith(".d.ts") or any(x in s for x in _TS_SKIP):
                continue
            mods[str(p.relative_to(BASE_DIR).with_suffix("")).replace("\\", "/")] = p
    return mods


class FileIndex:
    """Structural index of a single Python file."""

    def __init__(self, module_name: str, filepath: Path):
        self.module_name  = module_name
        self.filepath     = filepath
        self.classes:    List[Dict] = []
        self.functions:  List[Dict] = []
        self.imports:    List[str]  = []
        self.constants:  List[str]  = []
        self.file_docstring = ""
        self.size_bytes  = 0
        self.last_modified = 0
        self.parse_error: Optional[str] = None

    def scan(self):
        if not self.filepath.exists():
            self.parse_error = "File not found"
            return
        stat = self.filepath.stat()
        self.size_bytes    = stat.st_size
        self.last_modified = stat.st_mtime
        try:
            source = self.filepath.read_text(encoding="utf-8")
            tree   = ast.parse(source)
        except Exception as e:
            self.parse_error = str(e)[:100]
            return

        self.file_docstring = ast.get_docstring(tree) or ""

        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.imports.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for alias in node.names:
                    self.imports.append(f"{module}.{alias.name}")
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id.isupper():
                        self.constants.append(target.id)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.functions.append(self._extract_function(node))
            elif isinstance(node, ast.ClassDef):
                cls_info = {
                    "name":    node.name,
                    "bases":   [self._name_from_node(b) for b in node.bases],
                    "docstring": (ast.get_docstring(node) or "")[:150],
                    "methods": [],
                }
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        cls_info["methods"].append(self._extract_function(item))
                self.classes.append(cls_info)

    def _extract_function(self, node) -> Dict:
        args = []
        for arg in node.args.args:
            if arg.arg != "self":
                ann = f": {self._name_from_node(arg.annotation)}" if arg.annotation else ""
                args.append(f"{arg.arg}{ann}")
        defaults = node.args.defaults
        if defaults:
            offset = len(args) - len(defaults)
            for i, default in enumerate(defaults):
                idx = offset + i
                if 0 <= idx < len(args):
                    args[idx] += f"={self._literal_value(default)}"
        return {
            "name":      node.name,
            "args":      args,
            "async":     isinstance(node, ast.AsyncFunctionDef),
            "docstring": (ast.get_docstring(node) or "")[:120],
            "line":      node.lineno,
        }

    def _name_from_node(self, node) -> str:
        if isinstance(node, ast.Name):      return node.id
        if isinstance(node, ast.Attribute): return f"{self._name_from_node(node.value)}.{node.attr}"
        if isinstance(node, ast.Constant):  return repr(node.value)
        if isinstance(node, ast.Subscript): return f"{self._name_from_node(node.value)}[...]"
        return "?"

    def _literal_value(self, node) -> str:
        if isinstance(node, ast.Constant): r = repr(node.value); return r[:20]+"..." if len(r)>20 else r
        if isinstance(node, ast.Name):     return node.id
        if isinstance(node, (ast.List, ast.Tuple)): return "[]" if isinstance(node, ast.List) else "()"
        if isinstance(node, ast.Dict):     return "{}"
        if isinstance(node, ast.Call):     return f"{self._name_from_node(node.func)}()"
        return "..."

    def to_compact_string(self) -> str:
        lines = [f"## {self.module_name}",
                 f"   Path: {self.filepath.name} ({self.size_bytes // 1024}KB)"]
        if self.parse_error:
            lines.append(f"   ⚠ Parse error: {self.parse_error}")
            return "\n".join(lines)
        if self.file_docstring:
            first_line = self.file_docstring.split("\n")[0].strip()
            if first_line:
                lines.append(f"   Purpose: {first_line}")
        internal = [i for i in self.imports if i.startswith(("shared.", "config", "memory.", "services."))]
        if internal:
            lines.append(f"   Depends on: {', '.join(internal[:6])}")
        for cls in self.classes:
            bases = f"({', '.join(cls['bases'])})" if cls['bases'] else ""
            lines.append(f"   class {cls['name']}{bases}")
            if cls['docstring']:
                lines.append(f"      \"{cls['docstring'][:80]}\"")
            for method in cls['methods']:
                prefix   = "async " if method['async'] else ""
                args_str = ", ".join(method['args'][:4])
                if len(method['args']) > 4: args_str += ", ..."
                lines.append(f"      {prefix}def {method['name']}({args_str}) @L{method['line']}")
        for func in self.functions:
            prefix   = "async " if func['async'] else ""
            args_str = ", ".join(func['args'][:4])
            if len(func['args']) > 4: args_str += ", ..."
            lines.append(f"   {prefix}def {func['name']}({args_str}) @L{func['line']}")
        if self.constants:
            lines.append(f"   Constants: {', '.join(self.constants[:10])}")
        return "\n".join(lines)

    def to_dict(self) -> Dict:
        return {
            "module":        self.module_name,
            "path":          str(self.filepath),
            "size_bytes":    self.size_bytes,
            "last_modified": self.last_modified,
            "classes":       self.classes,
            "functions":     self.functions,
            "imports":       self.imports,
            "constants":     self.constants,
            "docstring":     self.file_docstring[:200],
            "error":         self.parse_error,
        }


class TSFileIndex(FileIndex):
    """Lightweight regex index for TS/TSX (the gen-2 React cockpit). Same dict shape as FileIndex,
    no AST — captures top-level functions, React components (const X = () =>), and resolves relative
    imports to dotted module names so loadGraph() forms the cockpit's internal dependency edges."""

    def scan(self):
        import re
        if not self.filepath.exists():
            self.parse_error = "File not found"
            return
        stat = self.filepath.stat()
        self.size_bytes = stat.st_size
        self.last_modified = stat.st_mtime
        try:
            src = self.filepath.read_text(encoding="utf-8")
        except Exception as e:
            self.parse_error = str(e)[:100]
            return
        lead = re.match(r"\s*(?:/\*(.*?)\*/|//([^\n]*))", src, re.DOTALL)
        if lead:
            self.file_docstring = (lead.group(1) or lead.group(2) or "").strip().replace("*", "")[:200]
        # top-level `function X(...)` declarations
        for fm in re.finditer(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z0-9_]+)\s*\(([^)]*)\)", src, re.MULTILINE):
            args = [a.strip().split(":")[0].strip() for a in fm.group(2).split(",") if a.strip()]
            self.functions.append({"name": fm.group(1), "args": args[:4], "async": "async" in fm.group(0),
                                   "docstring": "", "line": src[:fm.start()].count("\n") + 1})
        # `const X = (...) =>` arrow declarations (React components / helpers)
        for cm in re.finditer(r"^\s*(?:export\s+)?const\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:async\s*)?\([^)]*\)\s*(?::[^=]+?)?=>", src, re.MULTILINE):
            self.functions.append({"name": cm.group(1), "args": [], "async": "async" in cm.group(0),
                                   "docstring": "", "line": src[:cm.start()].count("\n") + 1})
        seen = set()
        self.functions = [f for f in self.functions if not (f["name"] in seen or seen.add(f["name"]))]
        # imports: resolve relative specs to dotted module names (edge-matching), keep externals as-is
        mod_dir = self.module_name.rsplit("/", 1)[0]
        for im in re.finditer(r"""import\s+[^'"]*from\s+['"]([^'"]+)['"]""", src):
            spec = im.group(1)
            if spec.startswith("."):
                parts = mod_dir.split("/")
                for seg in spec.split("/"):
                    if seg in (".", ""):
                        continue
                    if seg == "..":
                        parts = parts[:-1]
                    else:
                        parts.append(seg)
                self.imports.append("/".join(parts).replace("/", "."))
            else:
                self.imports.append(spec)


class CodebaseIndex:
    """Structural map of the RouxYou codebase for Coder context injection."""

    def __init__(self):
        self.files:    Dict[str, FileIndex] = {}
        self.built_at: float = 0
        self._build()

    def _build(self):
        start = time.time()
        self.files = {}  # reset so a rebuild drops modules that vanished
        for module_name, filepath in _discover_modules().items():
            try:
                idx = FileIndex(module_name, filepath)
                idx.scan()
                self.files[module_name] = idx
            except Exception as e:  # one unparseable file shouldn't sink the whole index
                print(f"CODEBASE INDEX: skipped {module_name}: {e}")
        try:  # gen-2 React cockpit (dashboard-next/) — isolated so a TS hiccup never sinks the Py index
            for module_name, filepath in _discover_ts_modules().items():
                try:
                    idx = TSFileIndex(module_name, filepath)
                    idx.scan()
                    self.files[module_name] = idx
                except Exception as e:
                    print(f"CODEBASE INDEX: skipped TS {module_name}: {e}")
        except Exception as e:
            print(f"CODEBASE INDEX: TS discovery failed: {e}")
        self.built_at = time.time()
        elapsed        = time.time() - start
        total_classes  = sum(len(f.classes)   for f in self.files.values())
        total_funcs    = sum(len(f.functions) for f in self.files.values())
        total_methods  = sum(sum(len(c["methods"]) for c in f.classes) for f in self.files.values())
        print(f"CODEBASE INDEX: Scanned {len(self.files)} modules in {elapsed:.2f}s "
              f"({total_classes} classes, {total_methods} methods, {total_funcs} functions)")
        self._cache()

    def _cache(self):
        try:
            INDEX_CACHE.parent.mkdir(parents=True, exist_ok=True)
            data = {"built_at": self.built_at,
                    "modules": {name: idx.to_dict() for name, idx in self.files.items()}}
            with open(INDEX_CACHE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"CODEBASE INDEX: Cache write failed: {e}")

    def refresh_if_stale(self, max_age_seconds: int = 300):
        mods = {**_discover_modules(), **_discover_ts_modules()}
        changed = any(fp.exists() and fp.stat().st_mtime > self.built_at for fp in mods.values())
        new_or_gone = set(mods.keys()) != set(self.files.keys())  # also catch added/removed files
        if changed or new_or_gone:
            print(f"CODEBASE INDEX: tree changed (mtime={changed}, set-delta={new_or_gone}), rebuilding...")
            self._build()
            self._cache()
            return True
        return False

    def get_system_map(self, compact: bool = True) -> str:
        return self._compact_map() if compact else self._full_map()

    def _compact_map(self) -> str:
        lines = ["=== ROUXYOU SYSTEM FILES ==="]
        groups = {
            "Agents":   ["orchestrator", "coder", "worker", "gateway"],
            "Services": ["services/roux", "services/watchtower"],
            "Memory":   ["memory/memory_agent", "memory/http_api"],
            "Shared":   [k for k in self.files if k.startswith("shared/")],
            "UI":       ["dashboard"],
            "Dashboard (React cockpit)": [k for k in self.files if k.startswith("dashboard-next/")],
        }
        for group_name, modules in groups.items():
            lines.append(f"[{group_name}]")
            for mod in modules:
                if mod not in self.files:
                    continue
                idx = self.files[mod]
                try:
                    path = str(idx.filepath.relative_to(BASE_DIR))
                except ValueError:
                    path = idx.filepath.name
                cls_names    = [c['name'] for c in idx.classes]
                func_names   = [f['name'] for f in idx.functions if not f['name'].startswith('_')]
                method_names = [f"{c['name']}.{m['name']}"
                                for c in idx.classes
                                for m in c['methods']
                                if not m['name'].startswith('_')]
                parts = [f"  {mod} ({path})"]
                if cls_names:    parts.append(f"    classes:   {', '.join(cls_names)}")
                if method_names: parts.append(f"    methods:   {', '.join(method_names[:8])}")
                if func_names:   parts.append(f"    functions: {', '.join(func_names[:6])}")
                lines.extend(parts)
        return "\n".join(lines)

    def _full_map(self) -> str:
        sections = ["=== ROUXYOU: SYSTEM ARCHITECTURE ===",
                    f"(Auto-generated, {len(self.files)} modules)\n"]
        groups = {
            "Core Agents":            ["orchestrator", "coder", "worker", "gateway"],
            "Services":               ["services/roux", "services/watchtower"],
            "Memory & RAG":           ["memory/memory_agent", "memory/http_api"],
            "Shared Infrastructure":  [k for k in self.files if k.startswith("shared/")],
            "Interface":              ["dashboard"],
            "Dashboard (React cockpit)": [k for k in self.files if k.startswith("dashboard-next/")],
        }
        for group_name, modules in groups.items():
            sections.append(f"--- {group_name} ---")
            for mod in modules:
                if mod in self.files:
                    sections.append(self.files[mod].to_compact_string())
            sections.append("")
        return "\n".join(sections)

    def find_function(self, name: str) -> List[Tuple[str, Dict]]:
        results = []
        for module_name, idx in self.files.items():
            for func in idx.functions:
                if name.lower() in func["name"].lower():
                    results.append((module_name, func))
            for cls in idx.classes:
                for method in cls["methods"]:
                    if name.lower() in method["name"].lower():
                        results.append((f"{module_name}::{cls['name']}", method))
        return results

    def find_class(self, name: str) -> List[Tuple[str, Dict]]:
        return [(mn, cls) for mn, idx in self.files.items()
                for cls in idx.classes if name.lower() in cls["name"].lower()]

    def get_dependency_graph(self) -> Dict[str, List[str]]:
        graph = {}
        for module_name, idx in self.files.items():
            deps = []
            for imp in idx.imports:
                for other_name in self.files:
                    other_parts = other_name.replace("/", ".")
                    if other_parts in imp or imp.startswith(other_parts):
                        if other_name != module_name:
                            deps.append(other_name)
            graph[module_name] = list(set(deps))
        return graph


# Singleton — built once when imported
codebase_index = CodebaseIndex()
