"""Checks by rule on a finished change, before anything is written.

Two questions a model's change can be asked without running it:

- **Does it parse?** A Python file is parsed with `ast.parse`, a JSON or TOML
  file loaded. Parsing executes nothing, so "this platform never runs the code
  it writes" still holds. A file whose original did not parse either (another
  Python, a template) is not judged: the rule cannot tell its syntax from ours.
- **Does what it imports exist?** A Python import the change *adds* that names
  a module of this repository must find that module, and each name it imports
  must be defined there - read from the module as the change leaves it, else as
  the run read it, else as indexed. A live test author wrote tests against
  functions and a table that did not exist; until now only the review agent's
  instructions stood in the way.

Both refuse only what they can be sure of. An import already in the original
file is not the change's doing and is left alone. Nothing outside the
repository is judged (a module is the repository's only when its top-level
package is, at exactly one root). An import guarded by `except ImportError`
is optional by design. A module that defines `__getattr__`, star-imports or
writes to `globals()` can hold any name. A module missing from the repository
counts only when the snapshot was indexed completely. Everything here is pure:
the caller hands in the paths and texts, so nothing reads a database or a disk.
"""

import ast
import json
import posixpath
import tomllib

#: What the rules can parse, by extension, and how.
PYTHON = (".py",)


def _python_error(text):
    try:
        ast.parse(text)
    except SyntaxError as error:
        return f"line {error.lineno}: {error.msg}"
    except ValueError as error:  # a null byte
        return str(error)
    return None


def _json_error(text):
    try:
        json.loads(text)
    except ValueError as error:
        return str(error)
    return None


def _toml_error(text):
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        return str(error)
    return None


PARSERS = {
    ".py": ("Python", _python_error),
    ".json": ("JSON", _json_error),
    ".toml": ("TOML", _toml_error),
}


def syntax_problem(path, content, original=None):
    """Why `content` does not parse as its file type, or None.

    `original` is the file before the change, when there was one. If that did
    not parse either, the file is not judged: a Python 2 module or a Jinja
    template named `.json` would otherwise be refused for being what it was.
    """
    found = PARSERS.get(posixpath.splitext(path)[1].lower())
    if found is None:
        return None
    language, error_of = found
    error = error_of(content)
    if error is None:
        return None
    if original and error_of(original) is not None:
        return None
    return f"{path}: does not parse as {language} ({error})."


# -- imports -----------------------------------------------------------------

#: Exception names whose handler makes an import inside its `try` optional.
OPTIONAL = {"ImportError", "ModuleNotFoundError", "Exception", "BaseException"}


def _optional(handler):
    """Whether this `except` clause catches a failed import."""
    if handler.type is None:
        return True
    kinds = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any(
        (isinstance(kind, ast.Name) and kind.id in OPTIONAL)
        or (isinstance(kind, ast.Attribute) and kind.attr in OPTIONAL)
        for kind in kinds
    )


def imports(text):
    """(level, module, names, line) for each import a file makes, or None
    when it does not parse.

    `names` is empty for `import a.b`, which needs only the module. Imports
    inside a `try` whose handler catches a failed import are left out.
    """
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    found = []

    def visit(node, guarded):
        if isinstance(node, ast.Import):
            if not guarded:
                for alias in node.names:
                    found.append((0, alias.name, (), node.lineno))
            return
        if isinstance(node, ast.ImportFrom):
            if not guarded:
                names = tuple(alias.name for alias in node.names if alias.name != "*")
                found.append((node.level, node.module or "", names, node.lineno))
            return
        if isinstance(node, ast.Try | ast.TryStar):
            optional = guarded or any(_optional(handler) for handler in node.handlers)
            for child in node.body:
                visit(child, optional)
            for child in node.handlers + node.orelse + node.finalbody:
                visit(child, guarded)
            return
        for child in ast.iter_child_nodes(node):
            visit(child, guarded)

    visit(tree, False)
    return found


def _bind(target, names):
    if isinstance(target, ast.Name):
        names.add(target.id)
    elif isinstance(target, ast.Tuple | ast.List):
        for element in target.elts:
            _bind(element, names)
    elif isinstance(target, ast.Starred):
        _bind(target.value, names)


def defined_names(text):
    """(names, open) for a module: what it binds at module level, and whether
    it can hold names no rule can see. None when it does not parse."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    names, unbounded = set(), "globals()" in text

    def visit(body):
        nonlocal unbounded
        for node in body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                names.add(node.name)
                if node.name == "__getattr__":
                    unbounded = True
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    _bind(target, names)
            elif isinstance(node, ast.AnnAssign | ast.AugAssign):
                _bind(node.target, names)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    names.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name == "*":
                        unbounded = True
                    else:
                        names.add(alias.asname or alias.name)
            elif isinstance(node, ast.For | ast.AsyncFor):
                _bind(node.target, names)
                visit(node.body + node.orelse)
            elif isinstance(node, ast.With | ast.AsyncWith):
                for item in node.items:
                    if item.optional_vars is not None:
                        _bind(item.optional_vars, names)
                visit(node.body)
            elif isinstance(node, ast.If | ast.While):
                visit(node.body + node.orelse)
            elif isinstance(node, ast.Try | ast.TryStar):
                for handler in node.handlers:
                    if handler.name:
                        names.add(handler.name)
                    visit(handler.body)
                visit(node.body + node.orelse + node.finalbody)
            elif isinstance(node, ast.TypeAlias):
                names.add(node.name.id)
            elif isinstance(node, ast.Match):
                # Its patterns bind names; rather than read them, treat the
                # module as able to hold anything.
                unbounded = True

    visit(tree.body)
    # A walrus binds wherever it stands. Every one is counted, which can only
    # make the check say less.
    names.update(
        node.target.id for node in ast.walk(tree) if isinstance(node, ast.NamedExpr)
    )
    return names, unbounded


def top_level_packages(paths):
    """{package name: {root}} for every package directly under a root.

    A package is a directory with an `__init__.py`; it is top level when its
    parent has none. Its parent is then a root: where an interpreter would
    have to be pointed for `import <package>` to find it.
    """
    initialised = {
        posixpath.dirname(path) for path in paths if posixpath.basename(path) == "__init__.py"
    }
    packages = {}
    for directory in initialised:
        if not directory:
            continue
        parent = posixpath.dirname(directory)
        if parent in initialised:
            continue
        packages.setdefault(posixpath.basename(directory), set()).add(parent)
    return packages


def _module_file(base, paths):
    """The file holding the module at `base` (a path without extension)."""
    for candidate in (f"{base}.py", f"{base}/__init__.py"):
        if candidate in paths:
            return candidate
    return None


def _join(*parts):
    return "/".join(part for part in parts if part)


def resolve(path, level, module, paths, packages):
    """Where an import in `path` points: (base, file) inside the repository,
    (base, None) for a module of this repository that is not there, or None
    when the module is not this repository's to judge."""
    parts = module.split(".") if module else []
    if level:
        directory = posixpath.dirname(path)
        if _join(directory, "__init__.py") not in paths:
            return None
        for _ in range(level - 1):
            directory = posixpath.dirname(directory)
        base = _join(directory, *parts)
    else:
        roots = packages.get(parts[0]) if parts else None
        if not roots or len(roots) != 1:
            return None
        base = _join(next(iter(roots)), *parts)
    if not base:
        return None
    return base, _module_file(base, paths)


def import_problems(changes, originals, paths, texts, complete=True):
    """What the change's new Python imports name that the repository lacks.

    `changes` are the files as the change leaves them ({"path", "content"});
    `originals` maps a path to its text before the change; `paths` is every
    file in the repository as indexed, to which the change's own paths are
    added; `texts(paths)` returns {path: text} for module files the change
    does not hold. `complete` is whether the index read the whole repository:
    if not, a module that is missing may simply not have been read.
    """
    changed = {change["path"]: change["content"] for change in changes}
    known = set(paths) | set(changed)
    packages = top_level_packages(known)
    needs = []
    for path, content in changed.items():
        if posixpath.splitext(path)[1] not in PYTHON:
            continue
        found = imports(content)
        if found is None:
            continue  # the syntax check says why
        before = set()
        if originals.get(path):
            before = {
                (level, module, name)
                for level, module, names, _ in imports(originals[path]) or []
                for name in names or ("",)
            }
        for level, module, names, line in found:
            fresh = tuple(name for name in names if (level, module, name) not in before)
            if names and not fresh:
                continue
            if not names and (level, module, "") in before:
                continue
            target = resolve(path, level, module, known, packages)
            if target is not None:
                needs.append((path, line, level, module, fresh, target))
    wanted = {target[1] for *_, target in needs if target[1] and target[1] not in changed}
    held = dict(texts(sorted(wanted))) if wanted else {}
    held.update(changed)
    definitions = {}
    problems = []
    for path, line, level, module, names, (base, module_path) in needs:
        shown = "." * level + module
        if module_path is None:
            if complete:
                problems.append(
                    f"{path} line {line}: imports {shown}, which is not in the repository."
                )
            continue
        if not names:
            continue
        if module_path not in definitions:
            text = held.get(module_path)
            definitions[module_path] = defined_names(text) if text is not None else None
        defined = definitions[module_path]
        if defined is None or defined[1]:
            continue
        package = module_path.endswith("/__init__.py")
        missing = [
            name
            for name in names
            if name not in defined[0]
            and not (package and _module_file(_join(base, name), known))
        ]
        if missing:
            problems.append(
                f"{path} line {line}: imports {', '.join(missing)} from {shown}, "
                f"which {module_path} does not define."
            )
    return problems
