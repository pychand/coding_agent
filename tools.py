

from pathlib import Path
import ast
import re
from typing import Callable
import difflib
import json
import os
import subprocess
import shlex
import urllib.request


class ToolError(Exception):
    pass


# READ_ONLY_COMMANDS = {"ls", "cat", "head", "tail", "wc", "grep", "find", "pwd"}
# APPROVAL_COMMANDS = {"pytest"}  # runs project code, so you approve each run
# FORBIDDEN_CHARS = set("|;&<>`$\n")
READ_ONLY_COMMANDS = {"ls", "cat", "head", "tail", "wc", "grep", "find", "pwd", "python" , "pip3", "pip","python3"}
APPROVAL_COMMANDS = {"pytest"}   # runs project code, so you approve each run
FORBIDDEN_CHARS = set()
BLOCKED_FIND_OPTIONS = {"-exec", "-execdir", "-delete", "-ok", "-okdir", "-fprint", "-fprintf", "-fls"}
SECRET_NAMES = {".env", ".ssh", ".aws", ".git", "id_rsa", "id_ed25519"}
SECRET_SUFFIXES = (".pem", ".key")
MAX_OUTPUT_CHARS = 10_000  # long outputs are cut so they don't flood the model's context
COMMAND_TIMEOUT = 30  # seconds
CONTAINER_IMAGE = os.getenv("MINI_CURSOR_CONTAINER_IMAGE", "python:3.12-slim")

def _function(name: str, description: str, properties: dict) -> dict:
    return {
        "type": "function",
        "name": name,
        "description": description,
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        },
    }


PATH = {"type": "string", "description": "Path relative to the workspace root"}

TOOL_SCHEMAS = [
    _function("list_files", "List the files and folders inside a folder of the workspace.", {"path": PATH}),
    _function("read_file", "Read a text file from the workspace.", {"path": PATH}),
    _function("find_files", "Find files by glob pattern, e.g. '**/*.py' or 'tests/test_*.py'.",
              {"pattern": {"type": "string", "description": "Glob pattern relative to the workspace root"}}),
    _function("search_code", "Search Python code by function and class names using the AST, not text grep.",
              {"query": {"type": "string", "description": "Case-insensitive substring of a function or class name"},
               "path": {"type": "string", "description": "Workspace-relative Python file or folder; use '.' for the whole workspace"}}),
    _function("edit_file", "Make one small exact-text replacement in a text file. old must match exactly once.",
              {"path": PATH,
               "old": {"type": "string", "description": "The exact text to replace"},
               "new": {"type": "string", "description": "The replacement text"}}),
    _function("write_file", "Create or overwrite a file with the FULL new content. The user must approve the change.",
              {"path": PATH, "content": {"type": "string", "description": "The complete new file content"}}),
    _function("run_command",
              "Run command inside a network-isolated Docker container with the workspace mounted. Before running any commands/files , make sure to install dependencies. Make sure you have multiple commands in one line since docker is exited when command execution is completed. "
              "Allowed: all but user must approve). ",
            #   "No pipes, redirects or chaining.",
            #   {"command": {"type": "string", "description": "A single command, e.g. 'pytest -q' or 'grep -n def app.py'"}}),
              {"command": {"type": "string", "description": "an allowed command"}}),
    _function("web_search", "Search the web with Tavily for current information.",
              {"query": {"type": "string", "description": "The search query"}}),
]

class Toolbox:
    def __init__(self, root: str,approve: Callable[[str], bool]):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.approve = approve
        self.checkpoints = []


    # -- git checkpoints ----------------------------------------------------
    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT,
        )

    def _ensure_git_repo(self) -> bool:
        result = self._git("rev-parse", "--is-inside-work-tree")
        if result.returncode == 0:
            return True

        initialized = self._git("init")
        if initialized.returncode != 0:
            raise ToolError(f"Could not initialize Git checkpoints: {initialized.stderr.strip()}")
        return True

    def _checkpoint(self, file: Path) -> None:
        self._ensure_git_repo()

        relative = file.relative_to(self.root).as_posix()
        if file.exists():
            staged = self._git("add", "--", relative)
            if staged.returncode != 0:
                raise ToolError(f"Could not stage checkpoint for {relative}: {staged.stderr.strip()}")

        commit = self._git(
            "-c", "user.name=Mini Cursor",
            "-c", "user.email=mini-cursor@localhost",
            "commit", "--allow-empty", "-m",
            f"Mini Cursor checkpoint: before {relative}",
        )
        if commit.returncode != 0:
            raise ToolError(f"Could not create checkpoint for {relative}: {commit.stderr.strip()}")

        head = self._git("rev-parse", "HEAD")
        if head.returncode != 0:
            raise ToolError("Could not record checkpoint commit.")
        self.checkpoints.append({
            "hash": head.stdout.strip(),
            "path": relative,
        })

    def undo(self) -> str:
        if not self.checkpoints:
            raise ToolError("There is no approved change to undo.")

        checkpoint = self.checkpoints.pop()
        self._ensure_git_repo()

        relative = checkpoint["path"]
        file = self.root / Path(relative)
        committed = self._git("cat-file", "-e", f"{checkpoint['hash']}:{relative}")
        if committed.returncode == 0:
            restored = self._git("restore", "--source", checkpoint["hash"], "--worktree", "--", relative)
            if restored.returncode != 0:
                raise ToolError(f"Could not undo {relative}: {restored.stderr.strip()}")
        elif file.exists():
            file.unlink()

        return f"Undid the last change to {relative}."


    # -- guards --------------------------------------------------------------
    def safe_path(self, path: str) -> Path:
        """Resolve a path and make sure it stays inside the workspace and isn't a secret."""
        resolved = (self.root / path).resolve()
        if not resolved.is_relative_to(self.root):
            raise ToolError(f"'{path}' is outside the workspace. Use paths inside {self.root.name}/.")
        relative_parts = resolved.relative_to(self.root).parts
        if any(part in SECRET_NAMES or part.endswith(SECRET_SUFFIXES) for part in relative_parts):
            raise ToolError(f"'{path}' looks like a secret (keys, .env, .git). Access denied.")
        return resolved

    # tools

    def is_secret(self, path: Path) -> bool:
        return any(part in SECRET_NAMES or part.endswith(SECRET_SUFFIXES)
                   for part in path.relative_to(self.root).parts)
    
    def list_files(self,path: str) -> str:
        folder = self.safe_path(path)
        if not folder.is_dir():
            raise ToolError(f"{path} is not a folder")

        entries = []
        for item in sorted(folder.iterdir(),key=lambda p: (p.is_file(),p.name)):
            if self.is_secret(item) or item.name in ("__pycache__",".pytest_cache"):
                continue
            entries.append(f"{item.name}/" if item.is_dir() else f"{item.name}  ({item.stat().st_size} bytes)")

        return "\n".join(entries) or "(empty folder)"

    def read_file(self,path: str) -> str:
        file = self.safe_path(path)
        if not file.is_file():
            raise ToolError(f"{path} does not exist or is not a file.")
        return file.read_text(encoding="utf-8",errors="replace")

    def find_files(self,pattern: str) -> str:
        if pattern.startswith("/") or ".." in pattern:
            raise ToolError("Patterns must be relative to the workspace (no '/' at the start, no '..').")

        matches = [p for p in sorted(self.root.glob(pattern))
                   if p.is_file() and not self.is_secret(p) and "__pycache__" not in p.parts]

        return "\n".join(str(p.relative_to(self.root)) for p in matches[:200]) or "(no matches)"

    def search_code(self, query: str, path: str) -> str:
        if not query.strip():
            raise ToolError("The search query cannot be empty.")

        target = self.safe_path(path)
        if target.is_file():
            if target.suffix.lower() != ".py":
                raise ToolError("search_code only supports Python source files (.py).")
            files = [target]
        elif target.is_dir():
            files = sorted(target.rglob("*.py"))
        else:
            raise ToolError(f"{path} does not exist or is not a file or folder.")

        matches = []
        parse_errors = []
        needle = query.strip().casefold()

        class SymbolVisitor(ast.NodeVisitor):
            def __init__(self):
                self.scope = []
                self.matches = []

            def _visit_symbol(self, node, kind):
                qualified_name = ".".join(self.scope + [node.name])
                if needle in node.name.casefold():
                    self.matches.append((node.lineno, kind, qualified_name))
                self.scope.append(node.name)
                self.generic_visit(node)
                self.scope.pop()

            def visit_ClassDef(self, node):
                self._visit_symbol(node, "class")

            def visit_FunctionDef(self, node):
                self._visit_symbol(node, "function")

            def visit_AsyncFunctionDef(self, node):
                self._visit_symbol(node, "async function")

        for file in files:
            if self.is_secret(file) or "__pycache__" in file.parts or file.is_symlink():
                continue
            try:
                source = file.read_text(encoding="utf-8", errors="replace")
                tree = ast.parse(source, filename=str(file))
            except SyntaxError as error:
                relative = file.relative_to(self.root).as_posix()
                parse_errors.append(f"{relative}: unable to parse Python source at line {error.lineno}: {error.msg}")
                continue

            visitor = SymbolVisitor()
            visitor.visit(tree)
            relative = file.relative_to(self.root).as_posix()
            matches.extend(
                f"{relative}:{line}: {kind} {name}"
                for line, kind, name in visitor.matches
            )

        output = matches
        if parse_errors:
            output.extend(f"Skipped: {error}" for error in parse_errors)
        return "\n".join(output) or "(no matching functions or classes)"


    def write_file(self,path: str,content: str) -> str:
        file = self.safe_path(path)
        old = file.read_text(encoding="utf-8") if file.exists() else ""
        if old == content:
            return f"No change: {path} already has this content."
        # Show the human exactly what will change, as a unified diff.
        diff = "".join(difflib.unified_diff(
            old.splitlines(keepends=True), content.splitlines(keepends=True),
            fromfile=f"a/{path}" if old else "/dev/null", tofile=f"b/{path}"))
        
        if not self.approve(f"write {path}\n{diff}"):
            return f"The user REJECTED the change to {path}. Ask what they want instead."
        self._checkpoint(file)
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content, encoding="utf-8")
        return f"Wrote {path} ({len(content.splitlines())} lines)."

    def edit_file(self, path: str, old: str, new: str) -> str:
        file = self.safe_path(path)
        if not file.is_file():
            raise ToolError(f"{path} does not exist or is not a file.")
        if old == "":
            raise ToolError("The old text cannot be empty.")

        content = file.read_text(encoding="utf-8")
        count = content.count(old)
        if count == 0:
            raise ToolError(f"The exact text was not found in {path}.")
        if count > 1:
            raise ToolError(f"The exact text occurs {count} times in {path}; provide more surrounding context.")

        updated = content.replace(old, new, 1)
        if updated == content:
            return f"No change: {path} already has the requested text."

        diff = "".join(difflib.unified_diff(
            content.splitlines(keepends=True), updated.splitlines(keepends=True),
            fromfile=f"a/{path}", tofile=f"b/{path}"))
        if not self.approve(f"edit {path}\n{diff}"):
            return f"The user REJECTED the change to {path}. Ask what they want instead."

        self._checkpoint(file)
        file.write_text(updated, encoding="utf-8")
        return f"Edited {path}."


    def run_command(self, command: str) -> str:
        if any(ch in FORBIDDEN_CHARS for ch in command):
            raise ToolError("Pipes, redirects, chaining (; && |) and $ are not allowed. Send one simple command.")
        argv = shlex.split(command)
        if not argv:
            raise ToolError("Empty command.")
        program = argv[0]
        # if program not in READ_ONLY_COMMANDS | APPROVAL_COMMANDS:
        #     allowed = ", ".join(sorted(READ_ONLY_COMMANDS | APPROVAL_COMMANDS))
        #     raise ToolError(f"'{program}' is not allowed. Allowed commands: {allowed}.")
        if program == "find" and BLOCKED_FIND_OPTIONS & set(argv):
            raise ToolError("find may only search (no -exec, -delete or -fprint).")
        if program == "grep":  # a recursive grep must not read secret files
            argv[1:1] = ["--exclude=.env*", "--exclude=*.pem", "--exclude=*.key", "--exclude=id_*",
                         "--exclude-dir=.git", "--exclude-dir=.ssh", "--exclude-dir=.aws"]
        for arg in argv[1:]:  # any argument that looks like a path must stay inside the workspace
            if not arg.startswith("-") and ("/" in arg or arg.startswith(".") or (self.root / arg).exists()):
                self.safe_path(arg)
        if program in APPROVAL_COMMANDS and not self.approve(f"run `{command}`"):
            return f"The user REJECTED running `{command}`."
        container_command = [
            # "podman", "run", "--rm, ""--network", "none",
            # "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            # "--mount", f"type=bind,source={self.root},target=/workspace",
            # "--workdir", "/workspace",
            "podman", "run",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--mount", f"type=bind,source={self.root},target=/workspace",
            "--workdir", "/workspace",
            CONTAINER_IMAGE, *argv,
        ]
        completed = subprocess.run(
            container_command,
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT,
            shell=False,
        )
        print(completed.stdout)
        print(completed.stderr)
        output = (str(completed.stdout) + completed.stderr).strip() or "(no output)"
        if len(output) > MAX_OUTPUT_CHARS:
            output = output[:MAX_OUTPUT_CHARS] + "\n[output truncated]"
        return f"exit code {completed.returncode}\n{output}"

    def web_search(self, query: str) -> str:
        api_key = os.getenv("TAVILY_API_KEY")
        if not api_key:
            raise ToolError("TAVILY_API_KEY is not set. Add your Tavily API key to the environment or .env file.")
        if not query.strip():
            raise ToolError("Search query cannot be empty.")

        payload = json.dumps({
            "api_key": api_key,
            "query": query,
            "max_results": 5,
        }).encode("utf-8")
        request = urllib.request.Request(
            "https://api.tavily.com/search",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))

        results = data.get("results", [])
        if not results:
            return f"No results found for: {query}"

        formatted = []
        for result in results:
            title = result.get("title", "Untitled")
            url = result.get("url", "")
            content = result.get("content", "")
            formatted.append(f"{title}\n{url}\n{content}".strip())
        return "\n\n".join(formatted)

    # -- dispatcher ----------------------------------------------------------
    def run(self, name: str, args: dict) -> str:
        """Run a tool by name. Errors become text the model can read and react to."""
        tool = {"list_files": self.list_files, "read_file": self.read_file, "find_files": self.find_files,
                "search_code": self.search_code,
                "edit_file": self.edit_file, "write_file": self.write_file, "run_command": self.run_command,
                "web_search": self.web_search}.get(name)
        if tool is None:
            return f"error: unknown tool '{name}'"
        try:
            result = tool(**args)
        except ToolError as error:
            return f"error: {error}"
        except subprocess.TimeoutExpired:
            return f"error: command timed out after {COMMAND_TIMEOUT}s"
        except (OSError, ValueError) as error:  # ValueError: bad quotes in shlex
            return f"error: {error}"
        if len(result) > MAX_OUTPUT_CHARS:
            result = result[:MAX_OUTPUT_CHARS] + "\n[output truncated]"
        return result
