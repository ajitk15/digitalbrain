"""Bounded, deterministic static analysis for immutable repository snapshots."""

import ast
import hashlib
import posixpath
import re

SUPPORTED = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "jsx",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
}
SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".idea",
    ".vscode",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
    ".next",
    ".nuxt",
    "target",
    ".gradle",
    "build",
    "dist",
    "vendor",
    "coverage",
}
MAX_FILES = 500
MAX_FILE_BYTES = 400_000
MAX_TOTAL_BYTES = 20 * 1024 * 1024
ANALYZER_VERSION = "structural-v1"

JS_IMPORT = re.compile(
    r"(?:import\s+(?:[^;]*?\s+from\s+)?|export\s+[^;]*?\s+from\s+|require\s*\()"
    r"[\"']([^\"']+)[\"']"
)
JS_SYMBOL = re.compile(
    r"^\s*(?:export\s+)?(?:async\s+)?(?:function|class|interface|type|enum)\s+([A-Za-z_$][\w$]*)",
    re.MULTILINE,
)


def language_for(path):
    lower = path.lower()
    if lower.endswith(".d.ts"):
        return "typescript"
    dot = lower.rfind(".")
    return SUPPORTED.get(lower[dot:] if dot >= 0 else "")


def included(path, size):
    parts = path.replace("\\", "/").split("/")
    return (
        bool(language_for(path))
        and not any(part in SKIP_DIRS or part.startswith("cmake-build-") for part in parts)
        and 0 <= size <= MAX_FILE_BYTES
        and not parts[-1].endswith((".min.js", ".bundle.js"))
    )


def python_facts(path, text):
    symbols, imports = [], []
    try:
        tree = ast.parse(text, filename=path)
    except (SyntaxError, ValueError):
        return symbols, imports, False
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            symbols.append(
                {
                    "name": node.name,
                    "line": node.lineno,
                    "kind": "class" if isinstance(node, ast.ClassDef) else "function",
                }
            )
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(
                {"module": alias.name, "line": node.lineno, "level": 0} for alias in node.names
            )
        elif isinstance(node, ast.ImportFrom):
            imports.append(
                {
                    "module": node.module or "",
                    "names": [alias.name for alias in node.names],
                    "line": node.lineno,
                    "level": node.level,
                }
            )
    return symbols, imports, True


def js_facts(text):
    symbols = [
        {"name": match.group(1), "line": text.count("\n", 0, match.start()) + 1, "kind": "symbol"}
        for match in JS_SYMBOL.finditer(text)
    ]
    imports = [
        {"module": match.group(1), "line": text.count("\n", 0, match.start()) + 1, "level": 0}
        for match in JS_IMPORT.finditer(text)
    ]
    return symbols, imports, True


def facts(path, text):
    language = language_for(path)
    symbols, imports, parse_ok = (
        python_facts(path, text) if language == "python" else js_facts(text)
    )
    return {
        "path": path,
        "language": language,
        "digest": hashlib.sha256(text.encode()).hexdigest(),
        "content": text,
        "parse_ok": parse_ok,
        "symbols": symbols,
        "imports": imports,
        "lines": len(text.splitlines()),
    }


def _python_modules(paths):
    modules = {}
    for path in paths:
        if not path.endswith((".py", ".pyi")):
            continue
        module = re.sub(r"\.pyi?$", "", path).replace("/", ".")
        modules[module] = path
        if module.endswith(".__init__"):
            modules.setdefault(module[:-9], path)
    return modules


def _resolve_python(source, item, modules):
    directories = source.split("/")[:-1]
    level = item.get("level", 0)
    prefixes = (
        [".".join(directories[: max(0, len(directories) - (level - 1))])]
        if level
        else [".".join(directories[:depth]) for depth in range(len(directories), -1, -1)]
    )
    names = item.get("names") or []
    candidates = [f"{item['module']}.{name}".strip(".") for name in names]
    candidates.append(item["module"])
    for candidate in candidates:
        for prefix in prefixes:
            target = modules.get(f"{prefix}.{candidate}".strip("."))
            if target:
                return target
    return None


def _resolve_js(source, specifier, paths):
    if not specifier.startswith((".", "/", "@/", "~/")):
        return None
    if specifier.startswith(("@/", "~/")):
        bases = [posixpath.join(root, specifier[2:]) for root in ("src", "app", "lib", "")]
    else:
        bases = [posixpath.normpath(posixpath.join(posixpath.dirname(source), specifier))]
    extensions = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".d.ts")
    for base in bases:
        for candidate in [
            base,
            *(base + ext for ext in extensions),
            *(posixpath.join(base, "index") + ext for ext in extensions),
        ]:
            if candidate in paths:
                return candidate
    return None


def relationships(files):
    paths = {item["path"] for item in files}
    modules = _python_modules(paths)
    found = {}
    for item in files:
        for imported in item["imports"]:
            target = (
                _resolve_python(item["path"], imported, modules)
                if item["language"] == "python"
                else _resolve_js(item["path"], imported["module"], paths)
            )
            if target and target != item["path"]:
                found.setdefault((item["path"], target), []).append(imported.get("line", 0))
    return [
        {
            "source": source,
            "target": target,
            "kind": "import",
            "confidence": "static",
            "detail": "Imports project file",
            "evidence": {"lines": sorted(set(lines))},
        }
        for (source, target), lines in sorted(found.items())
    ]
