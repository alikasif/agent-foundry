"""AST static safety screening, shared by the generator and skill validation.

``screen`` parses Python source (never imports or runs it) and reports risky
constructs in five categories:

- ``network``: imports of ``socket``, ``ssl``, ``urllib``, ``urllib3``,
  ``requests``, ``httpx``, ``aiohttp``, ``http.client``, ``http.server``,
  ``ftplib``, ``smtplib``, ``poplib``, ``imaplib``, ``telnetlib``,
  ``xmlrpc``, ``websocket(s)`` (and dynamic imports of them), plus
  ``asyncio.open_connection`` / ``open_unix_connection`` / ``start_server``;
- ``subprocess``: ``subprocess``, ``pty``, ``multiprocessing``, ``posix``, ``nt``
  and ``_posixsubprocess`` imports,
  ``os.system``, ``os.popen``, ``os.exec*``, ``os.spawn*``, ``os.fork*``,
  ``asyncio.create_subprocess_exec`` / ``_shell``;
- ``delete``: ``os.remove``, ``os.unlink``, ``os.rmdir``, ``os.removedirs``,
  ``shutil.rmtree`` and any ``.unlink()`` / ``.rmdir()`` method call;
- ``dynamic_exec``: ``eval``, ``exec``, ``__import__``,
  ``compile``, ``types.FunctionType``, ``pickle`` / ``marshal`` loads,
  ``importlib.import_module``, the ``importlib.util`` / ``importlib.machinery``
  loaders (``spec_from_file_location``, ``module_from_spec``,
  ``SourceFileLoader``) and any ``.exec_module()`` / ``.load_module()`` call,
  ``runpy.run_path`` / ``run_module``, ``ctypes`` imports (native code is
  arbitrary execution), any reference to ``importlib.machinery.*_SUFFIXES``,
  ``sys.meta_path``, ``sys.path_hooks`` or ``sys.path_importer_cache``, and
  any reference to ``sys.path`` at all (reads, aliases, ``from sys import
  path``, mutation), any ``site`` / ``sitecustomize`` / ``usercustomize``
  import, relative imports and any binding of ``__path__`` / ``__package__``
  / ``__spec__``. ``hb_entry`` keeps the harness off ``sys.path``, so a tool
  cannot import a sibling or process a ``.pth`` file without one of these
  (T046 MJ5b, T048 MA1, T062 MJ1). Imports of stdlib modules that compile and
  run a string or file (``timeit``, ``code``, ``codeop``, ``pdb``, ``bdb``,
  ``cProfile``, ``profile``, ``trace``, ``doctest``, ``zipimport``,
  ``importlib._bootstrap*``, ``importlib.abc``, ``logging.config``,
  ``webbrowser``, ``_ctypes``), any reference to the ``importlib.machinery``
  file loaders and ``FileFinder``, and ``.get_code()`` / ``.source_to_code()``
  / ``.get_source()`` / ``.load_extension()`` calls are flagged too, so code
  stored in a non-``.py`` file cannot be run unseen (T062 MJ2, R2-MJ2);
- ``secret``: key-like string literals, string literals assigned to
  secret-looking names, and high-entropy string literals.

The screen is a heuristic. It cannot see through ``getattr`` tricks or data
flow, and it flags every delete because "outside the workdir" is not
decidable statically. Known limits: ``getattr(os, "system")`` style
indirection (including ``getattr(builtins, "ex" + "ec")`` and
``vars(builtins)["exec"]``), building a function from a code object with
``type(f)(code, {})()``, ``typing.get_type_hints`` and
``inspect.get_annotations(eval_str=True)`` over string annotations, other
deserialisers (``shelve``, ``dill``) and overwriting moves (``shutil.move``,
``os.replace``, ``os.rename``) are not reported. Only ``.py`` source is
screened: the caller must fail closed on ``.pth``/``.pyc``/``.so``/``.zip``
files (``spec.loader.unscreenable_files``). Each file is screened alone, so
the caller screens every ``*.py`` the container can see
(``spec.loader.unreferenced_py_files``). Pure stdlib leaf module.
"""

import ast
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

__all__ = ["Category", "Finding", "ScreenReport", "screen"]

Category = Literal["network", "subprocess", "delete", "dynamic_exec", "secret"]

NETWORK_MODULES = frozenset(
    {
        "socket",
        "ssl",
        "urllib",
        "urllib3",
        "requests",
        "httpx",
        "aiohttp",
        "ftplib",
        "smtplib",
        "poplib",
        "imaplib",
        "telnetlib",
        "xmlrpc",
        "websocket",
        "websockets",
    }
)
NETWORK_SUBMODULES = ("http.client", "http.server")
NETWORK_CALLS = frozenset(
    {"asyncio.open_connection", "asyncio.open_unix_connection", "asyncio.start_server"}
)
SUBPROCESS_MODULES = frozenset(
    {"subprocess", "pty", "multiprocessing", "posix", "nt", "_posixsubprocess"}
)
SUBPROCESS_CALLS = frozenset(
    {
        "os.system",
        "os.popen",
        "asyncio.create_subprocess_exec",
        "asyncio.create_subprocess_shell",
    }
)
SUBPROCESS_PREFIXES = ("os.exec", "os.spawn", "os.posix_spawn", "os.fork")
DYNAMIC_EXEC_MODULES = frozenset(
    {
        "ctypes",
        "site",
        "sitecustomize",
        "usercustomize",
        # Stdlib APIs that compile and run a string or file they are handed.
        "timeit",
        "code",
        "codeop",
        "pdb",
        "bdb",
        "cProfile",
        "profile",
        "trace",
        "doctest",
        "zipimport",
        "webbrowser",
        "_ctypes",
    }
)
DYNAMIC_EXEC_SUBMODULES = ("logging.config", "importlib._bootstrap", "importlib.abc")
IMPORT_BINDING_NAMES = frozenset({"__path__", "__package__", "__spec__"})
DELETE_CALLS = frozenset(
    {"os.remove", "os.unlink", "os.rmdir", "os.removedirs", "shutil.rmtree"}
)
DELETE_METHODS = frozenset({"unlink", "rmdir"})
DYNAMIC_EXEC_CALLS = frozenset(
    {
        "eval",
        "exec",
        "__import__",
        "builtins.eval",
        "builtins.exec",
        "builtins.__import__",
        "importlib.import_module",
        "runpy.run_path",
        "runpy.run_module",
        "importlib.util.spec_from_file_location",
        "importlib.util.spec_from_loader",
        "importlib.util.module_from_spec",
        "importlib.machinery.SourceFileLoader",
        "importlib.machinery.SourcelessFileLoader",
        "compile",
        "builtins.compile",
        "types.FunctionType",
        "typing.ForwardRef",
        "pkgutil.resolve_name",
        "pydoc.locate",
        "pydoc.pipepager",
        "pickle.load",
        "pickle.loads",
        "marshal.load",
        "marshal.loads",
    }
)
DYNAMIC_EXEC_METHODS = frozenset(
    {
        "exec_module",
        "load_module",
        # Loader methods that turn text or a file into a code object.
        "get_code",
        "source_to_code",
        "get_source",
        # sqlite3 native extension loading.
        "load_extension",
        "enable_load_extension",
    }
)
# Referencing these (not only calling them) can build a loader or finder.
LOADER_CLASSES = frozenset(
    f"importlib.machinery.{name}"
    for name in (
        "SourceFileLoader",
        "SourcelessFileLoader",
        "ExtensionFileLoader",
        "FileFinder",
    )
)
IMPORT_SYSTEM_NAMES = frozenset(
    {"sys.meta_path", "sys.path_hooks", "sys.path_importer_cache"}
)
SUFFIXES_PREFIX = "importlib.machinery."
SUFFIXES_SUFFIX = "_SUFFIXES"
SYS_PATH = "sys.path"

KEY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("sk- style API key", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)
SECRET_NAME = re.compile(r"(?i)(api_?key|secret|token|passw(or)?d|credential)")
MIN_NAMED_SECRET_LEN = 8
TOKEN_LIKE = re.compile(r"^[A-Za-z0-9+/=_\-]{20,}$")
HEX_LIKE = re.compile(r"^[0-9a-fA-F]{32,}$")
TOKEN_ENTROPY_BITS = 4.0
HEX_ENTROPY_BITS = 3.0


@dataclass(frozen=True)
class Finding:
    """One risky construct: its category, 1-based source line and a detail.

    ``detail`` never contains a full secret value; secrets are redacted.
    """

    category: Category
    line: int
    detail: str


@dataclass(frozen=True)
class ScreenReport:
    """Findings for one screened source, ordered by line then category."""

    findings: tuple[Finding, ...]
    path: Path | None = None

    @property
    def flagged(self) -> bool:
        """True when at least one finding exists."""
        return bool(self.findings)

    @property
    def categories(self) -> frozenset[Category]:
        """The distinct categories present in ``findings``."""
        return frozenset(f.category for f in self.findings)


def screen(target: Path | str) -> ScreenReport:
    """Statically screen Python code for risky constructs.

    Args:
        target: a ``Path`` to a ``.py`` file (read as UTF-8), or a ``str``
            holding Python source code. A ``str`` is always treated as
            source, never as a path.

    Returns:
        A ``ScreenReport``; clean code yields an empty ``findings`` tuple.

    Raises:
        SyntaxError: the source does not parse.
        OSError: the file cannot be read.
    """
    path = target if isinstance(target, Path) else None
    source = target.read_text(encoding="utf-8") if isinstance(target, Path) else target
    tree = ast.parse(source, filename=str(path) if path else "<source>")
    visitor = _Screener()
    visitor.visit(tree)
    findings = sorted(set(visitor.findings), key=lambda f: (f.line, f.category))
    return ScreenReport(findings=tuple(findings), path=path)


@dataclass
class _Screener(ast.NodeVisitor):
    """Collects findings; ``aliases`` maps local names to dotted origins."""

    findings: list[Finding] = field(default_factory=list)
    aliases: dict[str, str] = field(default_factory=dict)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            local = alias.asname or alias.name.split(".")[0]
            self.aliases[local] = alias.name if alias.asname else local
            self._check_module(alias.name, node.lineno, "import")

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if node.level > 0:
            self.findings.append(
                Finding("dynamic_exec", node.lineno, "relative import")
            )
        if node.level == 0:
            self._check_module(module, node.lineno, "from-import")
        for alias in node.names:
            origin = f"{module}.{alias.name}" if module else alias.name
            if node.level == 0 and origin.startswith(NETWORK_SUBMODULES):
                self._check_module(origin, node.lineno, "from-import")
            if node.level == 0 and origin.startswith(DYNAMIC_EXEC_SUBMODULES):
                self._check_module(origin, node.lineno, "from-import")
            if node.level == 0 and origin in LOADER_CLASSES:
                self.findings.append(
                    Finding("dynamic_exec", node.lineno, f"import {origin}")
                )
            if node.level == 0 and (_import_system(origin) or origin == SYS_PATH):
                self.findings.append(
                    Finding("dynamic_exec", node.lineno, f"import {origin}")
                )
            self.aliases[alias.asname or alias.name] = origin
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        name = self._qualified(node)
        if name is not None and _import_system(name):
            self._add("dynamic_exec", node, f"import system {name}")
        if name == SYS_PATH:
            self._add("dynamic_exec", node, f"reference {name}")
        if name is not None and name.startswith(DYNAMIC_EXEC_SUBMODULES):
            self._add("dynamic_exec", node, f"reference {name}")
        if name in LOADER_CLASSES:
            self._add("dynamic_exec", node, f"reference {name}")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Store) and node.id in IMPORT_BINDING_NAMES:
            self._add("dynamic_exec", node, f"binds {node.id}")

    def visit_Call(self, node: ast.Call) -> None:
        name = self._qualified(node.func)
        if name is not None:
            self._check_call(name, node)
        if isinstance(node.func, ast.Attribute) and node.func.attr in DELETE_METHODS:
            if name not in DELETE_CALLS:
                self._add("delete", node, f"call .{node.func.attr}()")
        if isinstance(node.func, ast.Attribute):
            if node.func.attr in DYNAMIC_EXEC_METHODS:
                self._add("dynamic_exec", node, f"call .{node.func.attr}()")
        for keyword in node.keywords:
            if keyword.arg is not None:
                self._check_named_secret(keyword.arg, keyword.value)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._check_named_secret(_target_name(target), node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self._check_named_secret(_target_name(node.target), node.value)
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict) -> None:
        for key, value in zip(node.keys, node.values, strict=True):
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                self._check_named_secret(key.value, value)
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if not isinstance(node.value, str):
            return
        for label, pattern in KEY_PATTERNS:
            match = pattern.search(node.value)
            if match:
                self._add("secret", node, f"{label} {_redact(match.group())}")
                return
        if _is_high_entropy(node.value):
            self._add("secret", node, f"high-entropy literal {_redact(node.value)}")

    def _check_module(self, module: str, line: int, how: str) -> None:
        top = module.split(".")[0]
        if top in NETWORK_MODULES or module.startswith(NETWORK_SUBMODULES):
            self.findings.append(Finding("network", line, f"{how} {module}"))
        if top in SUBPROCESS_MODULES:
            self.findings.append(Finding("subprocess", line, f"{how} {module}"))
        if top in DYNAMIC_EXEC_MODULES or module.startswith(DYNAMIC_EXEC_SUBMODULES):
            self.findings.append(Finding("dynamic_exec", line, f"{how} {module}"))

    def _check_call(self, name: str, node: ast.Call) -> None:
        if name in SUBPROCESS_CALLS or name.startswith(SUBPROCESS_PREFIXES):
            self._add("subprocess", node, f"call {name}()")
        if name in DELETE_CALLS:
            self._add("delete", node, f"call {name}()")
        if name in NETWORK_CALLS:
            self._add("network", node, f"call {name}()")
        if name not in DYNAMIC_EXEC_CALLS:
            return
        self._add("dynamic_exec", node, f"call {name}()")
        first = node.args[0] if node.args else None
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            self._check_module(first.value, node.lineno, f"dynamic import via {name}")

    def _check_named_secret(self, name: str | None, value: ast.expr) -> None:
        if name is None or not SECRET_NAME.search(name):
            return
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            return
        text = value.value
        if len(text) < MIN_NAMED_SECRET_LEN or any(c.isspace() for c in text):
            return
        self._add("secret", value, f"literal assigned to {name!r} {_redact(text)}")

    def _qualified(self, node: ast.expr) -> str | None:
        """Resolve ``a.b.c`` through import aliases; None for other callees."""
        if isinstance(node, ast.Name):
            return self.aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            base = self._qualified(node.value)
            return f"{base}.{node.attr}" if base else None
        return None

    def _add(self, category: Category, node: ast.expr, detail: str) -> None:
        self.findings.append(Finding(category, node.lineno, detail))


def _import_system(name: str) -> bool:
    """True for the import-machinery hooks and ``importlib.machinery.*_SUFFIXES``."""
    if name in IMPORT_SYSTEM_NAMES:
        return True
    return name.startswith(SUFFIXES_PREFIX) and name.endswith(SUFFIXES_SUFFIX)


def _target_name(target: ast.expr) -> str | None:
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return None


def _redact(text: str) -> str:
    return f"'{text[:4]}...' ({len(text)} chars)"


def _is_high_entropy(text: str) -> bool:
    """Token-shaped literal with letters, digits and high Shannon entropy."""
    if "://" in text:
        return False
    if HEX_LIKE.match(text):
        return _entropy(text) >= HEX_ENTROPY_BITS
    if not TOKEN_LIKE.match(text):
        return False
    has_letter = any(c.isalpha() for c in text)
    has_digit = any(c.isdigit() for c in text)
    return has_letter and has_digit and _entropy(text) >= TOKEN_ENTROPY_BITS


def _entropy(text: str) -> float:
    counts = Counter(text).values()
    return -sum(n / len(text) * math.log2(n / len(text)) for n in counts)
