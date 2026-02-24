#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GCLI - The Autonomous Terminal AI
Powered by Google Gemini with native Function Calling.
"""

import os
import sys
import subprocess
import time
import fnmatch
import traceback
import re
import json
import argparse
import copy
import uuid
import shlex
import shutil
import random
import threading
from pathlib import Path
from datetime import datetime

# ─── Constants ───────────────────────────────────────────────────────────────
_console = None

def get_console():
    global _console
    if _console is None:
        from rich.console import Console
        from rich.theme import Theme
        theme = Theme({
            "brand": "bold bright_cyan",
            "accent": "bold bright_blue",
            "muted": "dim",
            "ok": "bold bright_green",
            "warn": "bold yellow",
            "err": "bold red",
        })
        _console = Console(theme=theme)
    return _console

class LazyProxy:
    def __init__(self, loader):
        self._loader = loader
        self._obj = None
    def _get_obj(self):
        if self._obj is None:
            self._obj = self._loader()
        return self._obj
    def __getattr__(self, name):
        return getattr(self._get_obj(), name)
    def __call__(self, *args, **kwargs):
        return self._get_obj()(*args, **kwargs)
    def __getitem__(self, key):
        return self._get_obj()[key]
    def __setitem__(self, key, value):
        self._get_obj()[key] = value
    def __contains__(self, key):
        return key in self._get_obj()
    def __iter__(self):
        return iter(self._get_obj())
    def __len__(self):
        return len(self._get_obj())

def _load_gt():
    from google.genai import types
    return types

def _load_prompt():
    from rich.prompt import Prompt
    return Prompt

def _load_genai():
    from google import genai
    return genai

console = LazyProxy(get_console)
gt = LazyProxy(_load_gt)
Prompt = LazyProxy(_load_prompt)
genai = LazyProxy(_load_genai)

def rich_escape(text: str) -> str:
    from rich.markup import escape
    return escape(text)

def Markdown(text: str, **kwargs):
    from rich.markdown import Markdown
    return Markdown(text, **kwargs)

def Panel(*args, **kwargs):
    from rich.panel import Panel
    return Panel(*args, **kwargs)

def Table(*args, **kwargs):
    from rich.table import Table
    return Table(*args, **kwargs)

def Syntax(code: str, lexer: str, **kwargs):
    from rich.syntax import Syntax
    return Syntax(code, lexer, **kwargs)

def Live(*args, **kwargs):
    from rich.live import Live
    return Live(*args, **kwargs)

def Spinner(*args, **kwargs):
    from rich.spinner import Spinner
    return Spinner(*args, **kwargs)

def _load_text():
    from rich.text import Text
    return Text

Text = LazyProxy(_load_text)

def Rule(*args, **kwargs):
    from rich.rule import Rule
    return Rule(*args, **kwargs)


def _bootstrap_output_encoding() -> None:
    """Best-effort UTF-8 output on Windows to avoid Rich Unicode crashes."""
    if os.name != "nt":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

_SHELL_CMD = None
def _get_shell_cmd():
    global _SHELL_CMD
    if _SHELL_CMD is None:
        if shutil.which("pwsh"):
            _SHELL_CMD = "pwsh"
        else:
            _SHELL_CMD = "powershell"
    return _SHELL_CMD

_cwd = os.getcwd()
CONFIG_DIR = Path.home() / ".gcli"
SAVED_KEY_FILE = CONFIG_DIR / "apikey.txt"
AISTUDIO_URL = "https://aistudio.google.com/app/apikey"
SHOW_BANNER = True

# Gemini CLI integration paths
GEMINI_CLI_DIR = Path.home() / ".gemini"
GEMINI_CLI_ACCOUNTS = GEMINI_CLI_DIR / "google_accounts.json"
# Path to the helper script that reads the Gemini CLI's stored API key
_GEMINI_KEY_SCRIPT = Path(__file__).parent / "get_gemini_key.mjs"

PROVIDERS_KEY_FILE = CONFIG_DIR / "providers.json"

BANNER = r"""
   ____   ____ _     ___
  / ___| / ___| |   |_ _|
 | |  _ | |   | |    | |
 | |_| || |___| |___ | |
  \____| \____|_____|___|
"""
ASCII_BANNER = BANNER
UNICODE_OK = "utf" in ((getattr(sys.stdout, "encoding", "") or "").lower())

def _banner_text() -> str:
    return BANNER if UNICODE_OK else ASCII_BANNER

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="GCLI - The Autonomous Terminal AI powered by Google Gemini."
    )
    parser.add_argument("--model", help="Model ID to use (skips model picker).")
    parser.add_argument("--prompt", help="Run one prompt non-interactively, then exit.")
    parser.add_argument("--no-banner", action="store_true", help="Do not print startup banner.")
    parser.add_argument("--version", action="store_true", help="Show version and exit.")
    return parser.parse_args(argv)

APP_VERSION = "2.0.0"
STATE_FILE = CONFIG_DIR / "state.json"
SESSIONS_DIR = CONFIG_DIR / "sessions"
TRANSCRIPTS_DIR = CONFIG_DIR / "transcripts"

DEFAULT_PERSISTENT_STATE = {
    "settings": {
        "max_rounds": 150,
        "temperature": 0.3,
        "safe_mode": True,
        "auto_save_session": True,
        "show_stats_after_response": False,
        "history_preview_chars": 1000,
        "default_model": "",
    },
    "aliases": {},
    "snippets": {},
    "macros": {},
    "bookmarks": {},
    "profiles": {},
    "notes": [],
    "todos": [],
    "directory_permissions": {
        "mode": "prompt",
        "trusted_roots": [str(Path.home()), str(Path(_cwd).resolve())],
        "allow_once": [],
    },
}

SETTING_SPECS = {
    "max_rounds": ("int", 10, 500),
    "temperature": ("float", 0.0, 2.0),
    "safe_mode": ("bool",),
    "auto_save_session": ("bool",),
    "show_stats_after_response": ("bool",),
    "history_preview_chars": ("int", 100, 10000),
    "default_model": ("str",),
}

RUNTIME_STATE = {
    "session_id": datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8],
    "started_at": time.time(),
    "prompt_count": 0,
    "tool_call_count": 0,
    "tool_calls": {},
    "shell_history": [],
    "last_prompt": "",
    "last_response": "",
    "undo_stack": [],
    "redo_stack": [],
    "pinned": [],
    "session_tags": [],
}

# ─── Readline history (best-effort, no hard dep) ─────────────────────────────
_HISTORY_FILE = CONFIG_DIR / "history.txt"
_READLINE_OK = None # None means not yet checked
_rl = None

def _get_readline():
    global _READLINE_OK, _rl
    if _READLINE_OK is None:
        try:
            import readline as _rl
            _READLINE_OK = True
        except ImportError:
            try:
                import pyreadline3 as _rl  # type: ignore
                _READLINE_OK = True
            except ImportError:
                _READLINE_OK = False
    return _rl if _READLINE_OK else None

def _setup_readline() -> None:
    rl = _get_readline()
    if rl is None:
        return
    try:
        rl.set_history_length(1000)
        if _HISTORY_FILE.exists():
            rl.read_history_file(str(_HISTORY_FILE))
    except Exception:
        pass


def _save_readline_history() -> None:
    rl = _get_readline()
    if rl is None:
        return
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        rl.write_history_file(str(_HISTORY_FILE))
    except Exception:
        pass


def _deep_merge_dict(base: dict, incoming: dict) -> dict:
    for k, v in incoming.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge_dict(base[k], v)
        else:
            base[k] = v
    return base


def _load_persistent_state() -> dict:
    state = copy.deepcopy(DEFAULT_PERSISTENT_STATE)
    try:
        if STATE_FILE.exists():
            loaded = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                _deep_merge_dict(state, loaded)
    except Exception:
        pass
    return state


def _save_persistent_state() -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(PERSISTENT_STATE, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except Exception as e:
        console.print(f"[red]Failed to save state: {rich_escape(str(e))}[/red]")


PERSISTENT_STATE = LazyProxy(_load_persistent_state)
APP_SETTINGS = LazyProxy(lambda: PERSISTENT_STATE["settings"])


def _as_bool(value: str) -> bool:
    v = value.strip().lower()
    if v in ("1", "true", "yes", "y", "on", "enable", "enabled"):
        return True
    if v in ("0", "false", "no", "n", "off", "disable", "disabled"):
        return False
    raise ValueError("expected boolean value")


def _coerce_setting(key: str, raw: str):
    spec = SETTING_SPECS.get(key)
    if not spec:
        raise KeyError(f"Unknown setting: {key}")
    t = spec[0]
    if t == "bool":
        return _as_bool(raw)
    if t == "int":
        n = int(raw)
        if not (spec[1] <= n <= spec[2]):
            raise ValueError(f"{key} must be between {spec[1]} and {spec[2]}")
        return n
    if t == "float":
        x = float(raw)
        if not (spec[1] <= x <= spec[2]):
            raise ValueError(f"{key} must be between {spec[1]} and {spec[2]}")
        return x
    return raw


def _format_uptime(seconds: float) -> str:
    s = int(max(0, seconds))
    h, r = divmod(s, 3600)
    m, sec = divmod(r, 60)
    if h:
        return f"{h}h {m}m {sec}s"
    if m:
        return f"{m}m {sec}s"
    return f"{sec}s"


def _sanitize_name(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "_", name.strip())
    return cleaned[:80].strip("_") or "default"


def _session_path(name: str) -> Path:
    return SESSIONS_DIR / f"{_sanitize_name(name)}.json"


def _collect_session_payload(client) -> dict:
    return {
        "version": APP_VERSION,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "session_id": RUNTIME_STATE["session_id"],
        "cwd": _cwd,
        "model_id": client._model_id,
        "history": client._history,
        "runtime": {
            "prompt_count": RUNTIME_STATE["prompt_count"],
            "tool_call_count": RUNTIME_STATE["tool_call_count"],
            "pinned": RUNTIME_STATE["pinned"],
            "last_prompt": RUNTIME_STATE["last_prompt"],
            "last_response": RUNTIME_STATE["last_response"],
        },
    }


def _apply_session_payload(client, payload: dict) -> None:
    global _cwd
    history = payload.get("history", [])
    if isinstance(history, list):
        client._history = history
    mid = payload.get("model_id")
    if isinstance(mid, str) and mid.strip():
        client._model_id = mid.strip()
    cwd = payload.get("cwd")
    if isinstance(cwd, str):
        p = Path(cwd)
        if p.exists() and p.is_dir():
            ok, _ = _enforce_directory_access(_normalize_path(p), f"switch session cwd to {p}", for_write=False)
            if ok:
                _cwd = str(p.resolve())
    runtime = payload.get("runtime", {})
    if isinstance(runtime, dict):
        RUNTIME_STATE["prompt_count"] = int(runtime.get("prompt_count", RUNTIME_STATE["prompt_count"]))
        RUNTIME_STATE["tool_call_count"] = int(runtime.get("tool_call_count", RUNTIME_STATE["tool_call_count"]))
        RUNTIME_STATE["pinned"] = list(runtime.get("pinned", RUNTIME_STATE["pinned"]))
        RUNTIME_STATE["last_prompt"] = str(runtime.get("last_prompt", RUNTIME_STATE["last_prompt"]))
        RUNTIME_STATE["last_response"] = str(runtime.get("last_response", RUNTIME_STATE["last_response"]))


def _autosave_session(client) -> None:
    if not APP_SETTINGS.get("auto_save_session", True):
        return
    try:
        # Capture payload in main thread to avoid race conditions during history mutation
        payload = _collect_session_payload(client)
        
        def _save_task():
            try:
                SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
                (_session_path("autosave_latest")).write_text(
                    json.dumps(payload, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
            except Exception:
                pass
                
        # Fire and forget
        threading.Thread(target=_save_task, daemon=True).start()
    except Exception:
        pass


def _extract_last_model_text(history: list) -> str:
    for item in reversed(history):
        if item.get("role") != "model":
            continue
        for part in reversed(item.get("parts", [])):
            if "text" in part and part["text"].strip():
                return part["text"]
    return ""


def _record_tool_call(name: str) -> None:
    RUNTIME_STATE["tool_call_count"] += 1
    RUNTIME_STATE["tool_calls"][name] = RUNTIME_STATE["tool_calls"].get(name, 0) + 1


def _record_shell_command(command: str, exit_code: int, elapsed_sec: float, background: bool) -> None:
    RUNTIME_STATE["shell_history"].append({
        "time": datetime.now().strftime("%H:%M:%S"),
        "cwd": _cwd,
        "command": command,
        "exit_code": exit_code,
        "elapsed_sec": round(elapsed_sec, 3),
        "background": bool(background),
    })
    if len(RUNTIME_STATE["shell_history"]) > 200:
        RUNTIME_STATE["shell_history"] = RUNTIME_STATE["shell_history"][-200:]


def _is_dangerous_command(command: str) -> bool:
    if not APP_SETTINGS.get("safe_mode", True):
        return False
    patterns = [
        r"\bremove-item\b.+-recurse",
        r"\bdel\b\s+/[sqf]",
        r"\berase\b\s+/[sqf]",
        r"\brmdir\b\s+/[sq]",
        r"\bformat\b",
        r"\bdiskpart\b",
        r"\bshutdown\b",
        r"\brestart-computer\b",
        r"\breg\s+delete\b",
        r"\bsc\s+delete\b",
        r"\btaskkill\b.+/f",
    ]
    text = command.lower()
    return any(re.search(p, text) for p in patterns)


def _expand_alias(user_input: str) -> str:
    text = user_input.strip()
    if not text.startswith("/"):
        return user_input
    parts = text.split(" ", 1)
    cmd = parts[0][1:]
    rest = parts[1] if len(parts) > 1 else ""
    aliases = PERSISTENT_STATE.get("aliases", {})
    if cmd in aliases:
        mapped = aliases[cmd].strip()
        if mapped.startswith("/"):
            return mapped if not rest else f"{mapped} {rest}".strip()
        return f"/{mapped} {rest}".strip()
    return user_input


def _render_snippet(template: str, kv: dict) -> str:
    out = template
    out = out.replace("{cwd}", _cwd)
    out = out.replace("{date}", datetime.now().strftime("%Y-%m-%d"))
    out = out.replace("{time}", datetime.now().strftime("%H:%M:%S"))
    out = out.replace("{session_id}", RUNTIME_STATE["session_id"])
    for k, v in kv.items():
        out = out.replace("{" + k + "}", v)
    return out


def _capture_undo_snapshot(client, label: str) -> None:
    snap = {
        "label": label,
        "history": copy.deepcopy(client._history),
        "model_id": client._model_id,
        "cwd": _cwd,
    }
    RUNTIME_STATE["undo_stack"].append(snap)
    if len(RUNTIME_STATE["undo_stack"]) > 40:
        RUNTIME_STATE["undo_stack"] = RUNTIME_STATE["undo_stack"][-40:]
    RUNTIME_STATE["redo_stack"].clear()


def _perm_state() -> dict:
    perms = PERSISTENT_STATE.setdefault("directory_permissions", {})
    perms.setdefault("mode", "prompt")
    perms.setdefault("trusted_roots", [str(Path.home()), str(Path(_cwd).resolve())])
    perms.setdefault("allow_once", [])
    roots = []
    seen = set()
    for r in perms.get("trusted_roots", []):
        nr = str(_normalize_path(Path(r)))
        key = os.path.normcase(nr)
        if key not in seen:
            seen.add(key)
            roots.append(nr)
    perms["trusted_roots"] = roots
    return perms


def _normalize_path(p: Path) -> Path:
    try:
        return p.expanduser().resolve(strict=False)
    except TypeError:
        return p.expanduser().resolve()


def _resolve_user_path(path: str) -> Path:
    p = Path(path) if Path(path).is_absolute() else Path(_cwd) / path
    return _normalize_path(p)


def _path_within_root(path: Path, root: Path) -> bool:
    p = os.path.normcase(str(_normalize_path(path)))
    r = os.path.normcase(str(_normalize_path(root)))
    try:
        return os.path.commonpath([p, r]) == r
    except Exception:
        return False


def _permission_scope_for(path: Path, for_write: bool = False) -> Path:
    if path.exists():
        if path.is_dir():
            return _normalize_path(path)
        return _normalize_path(path.parent)
    if for_write:
        return _normalize_path(path.parent)
    return _normalize_path(path)


def _has_directory_access(scope: Path) -> bool:
    perms = _perm_state()
    if perms.get("mode") == "allow-all":
        return True
    for root in perms.get("trusted_roots", []):
        if _path_within_root(scope, Path(root)):
            return True
    for once in perms.get("allow_once", []):
        if _path_within_root(scope, Path(once)):
            return True
    return False


def _request_directory_access(scope: Path, action: str) -> bool:
    perms = _perm_state()
    if perms.get("mode") == "allow-all":
        return True
    if _has_directory_access(scope):
        return True
    if not sys.stdin.isatty():
        return False
    console.print(Panel(
        f"[bold]Action:[/bold] {rich_escape(action)}\n"
        f"[bold]Path:[/bold] {rich_escape(str(scope))}\n\n"
        "This location is outside trusted roots.",
        title="[yellow]Directory Permission Required[/yellow]",
        border_style="yellow",
    ))
    choice = Prompt.ask(
        "[bold]Allow once (y), trust permanently (t), deny (n)?[/bold]",
        choices=["y", "t", "n"],
        default="y",
    )
    if choice == "t":
        perms["trusted_roots"].append(str(scope))
        perms["trusted_roots"] = sorted(set(perms["trusted_roots"]))
        _save_persistent_state()
        return True
    if choice == "y":
        perms["allow_once"].append(str(scope))
        perms["allow_once"] = perms["allow_once"][-200:]
        return True
    return False


def _enforce_directory_access(path: Path, action: str, for_write: bool = False) -> tuple[bool, str]:
    scope = _permission_scope_for(path, for_write=for_write)
    if _request_directory_access(scope, action):
        return True, ""
    return False, (
        f"Permission denied for {scope}. Use /perm trust <path>, "
        "or /perm mode allow-all."
    )

SYSTEM_PROMPT = r"""You are GCLI, a powerful autonomous AI coding assistant running in the terminal — similar to Claude Code or OpenAI Codex CLI.

## Your Tools
- **run_command**: Execute PowerShell commands (installs, git, scripts, etc.)
- **read_file**: Read any file on disk (with optional line range)
- **write_file**: Create or completely overwrite a file
- **edit_file**: Surgical find-and-replace in a file
- **list_directory**: Explore directory structure
- **change_directory**: Change your working directory
- **search_files**: Recursively find files by name or content
- **search_web**: Search the internet using DuckDuckGo
- **read_url**: Read the plaintext contents of any webpage or documentation link

## Operating Principles
1. **Act, don't ask**: Full permission to use any tool. Never ask for confirmation.
2. **Be autonomous & persistent**: Complete tasks end-to-end. Keep iterating until it is fully solved.
3. **Iterate**: run command → read output → run next command. Chain tools freely.
4. **Testing code**: If you write code, you MUST immediately try to run or compile it to see if it works, and fix any errors. Don't assume code works.
5. **Optimal path**: Constantly look for the most native or optimal way to do things instead of complex messy workarounds.
6. **Follow Prompt Exactly**: Pay very close attention to EXACTLY what the user asks. If they ask for something to be built a specific way, do exactly that without deviating. Evaluate your finished work against their prompt.
7. **PowerShell syntax**: You're on Windows.
8. **Be concise**: One-line plan, use tools, brief summary of result.
9. **edit_file** for small changes, **write_file** when rewriting whole files.
10. **Think ahead**: Briefly stream your thought process before using tools. For complex tasks, write out a brief To-Do list of steps to accomplish the goal, and explicitly note backup plans ahead of time just in case your primary method fails.
11. **Find lost files**: If the user asks about a project or file but you don't know the exact path, use aggressive broad search commands (e.g. `cmd.exe /c "dir C:\*gcli* /s /b /ad"` or `Get-ChildItem -ErrorAction SilentlyContinue`) instead of blindly guessing paths.
12. **Error Recovery**: If a tool fails (e.g., run_command returns an error), DO NOT just give up. Read the error, explain what went wrong, and immediately execute your backup plan or try an alternative approach.
13. **Long Tasks & Planning**: For complex, multi-step coding tasks, you MUST first create a `gcli_plan.md` file using `write_file` outlining a step-by-step checklist. As you progress, use `edit_file` to mark steps as `[x]` to maintain state and avoid loops. Always review your plan if you lose track. Let user know when you finish a major step.
14. **Self-Correction**: If you get stuck in a loop of errors, STOP. Read the files completely to regain context, search the web for the exact error, re-evaluate your approach, update `gcli_plan.md` before continuing.
15. **Zero-Friction (Figure it out)**: NEVER ask the user for clarification about implementation details, tech stacks, or preferences unless 100% unavoidable. Make reasonable, intelligent assumptions and just execute it autonomously. Your goal is a fast, fluid, seamless experience without unnecessary dialogue.

OS: Windows | Shell: PowerShell | Working dir changes persist per session."""

# ══════════════════════════════════════════════════════════════════════════════
#  TOOL IMPLEMENTATIONS
# ══════════════════════════════════════════════════════════════════════════════

def _tool_run_command(command: str, timeout: int = 120, background: bool = False) -> dict:
    bg_tag = " [dim](background)[/dim]" if background else ""
    console.print(f"\n[bold yellow]$[/bold yellow]{bg_tag} [yellow]{rich_escape(command)}[/yellow]")

    ok, err = _enforce_directory_access(Path(_cwd), "execute command in current directory", for_write=True)
    if not ok:
        console.print(f"[red]✗ Permission denied: {rich_escape(err)}[/red]")
        return {"stdout": "", "stderr": err, "exit_code": -3, "success": False}

    if _is_dangerous_command(command):
        msg = "Blocked by safe_mode. Disable via '/set safe_mode false' if intentional."
        console.print("[yellow]⚠  Blocked by safe_mode[/yellow]")
        _record_shell_command(command, exit_code=-2, elapsed_sec=0.0, background=background)
        return {"stdout": "", "stderr": msg, "exit_code": -2, "success": False}

    try:
        shell = _get_shell_cmd()
        if background:
            encoded = command.replace('"', '""')
            full = f'Start-Process {shell} -ArgumentList "-NoLogo", "-NoExit", "-Command", "{encoded}"'
            subprocess.Popen([shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", full], cwd=_cwd)
            console.print("[green]✓ Spawned in background window[/green]")
            _record_shell_command(command, exit_code=0, elapsed_sec=0.0, background=True)
            return {"stdout": "Spawned detached window successfully.", "stderr": "", "exit_code": 0, "success": True}

        start = time.time()
        result = subprocess.run(
            [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, timeout=timeout, cwd=_cwd, encoding="utf-8", errors="replace"
        )
        elapsed = time.time() - start
        out = (result.stdout.strip() + ("\n" + result.stderr.strip() if result.stderr.strip() else "")).strip() or "(no output)"
        if len(out) > 4000:
            out = out[:2000] + "\n...[TRUNCATED]\n" + out[-2000:]
        ok = result.returncode == 0
        icon = "[green]✓[/green]" if ok else "[red]✗[/red]"
        console.print(f"{icon} [dim]exit {result.returncode} · {elapsed:.1f}s[/dim]")
        if out and out != "(no output)":
            console.print(rich_escape(out))
        _record_shell_command(command, exit_code=result.returncode, elapsed_sec=elapsed, background=False)
        return {"stdout": result.stdout.strip(), "stderr": result.stderr.strip(), "exit_code": result.returncode, "success": ok}
    except subprocess.TimeoutExpired:
        msg = f"Timed out after {timeout}s"
        console.print(f"[red]✗ Timed out after {timeout}s[/red]")
        _record_shell_command(command, exit_code=-1, elapsed_sec=float(timeout), background=False)
        return {"stdout": "", "stderr": msg, "exit_code": -1, "success": False}
    except Exception as e:
        console.print(f"[red]✗ {rich_escape(str(e))}[/red]")
        _record_shell_command(command, exit_code=-1, elapsed_sec=0.0, background=background)
        return {"stdout": "", "stderr": str(e), "exit_code": -1, "success": False}



def _tool_read_file(path: str, start_line: int = 1, end_line: int = 0) -> dict:
    console.print(f"[dim]read_file: [cyan]{rich_escape(path)}[/cyan][/dim]")
    try:
        p = _resolve_user_path(path)
        ok, err = _enforce_directory_access(p, f"read file {p}", for_write=False)
        if not ok:
            return {"success": False, "error": err}
        if not p.exists():
            return {"success": False, "error": f"Not found: {path}"}
        if not p.is_file():
            return {"success": False, "error": f"Not a file: {path}"}
        if p.stat().st_size > 5_000_000:
            return {"success": False, "error": "File too large (>5 MB). Use run_command."}
        content = p.read_text(encoding="utf-8", errors="replace")
        lines = content.splitlines()
        total = len(lines)
        snippet = "\n".join(lines[max(0, start_line - 1):(end_line or total)])
        lang = {"py": "python", "js": "javascript", "ts": "typescript", "html": "html", "css": "css",
                "json": "json", "md": "markdown", "sh": "bash", "ps1": "powershell",
                "yml": "yaml", "yaml": "yaml"}.get(p.suffix.lstrip("."), "text")
        preview = snippet[:3000] + ("\n...[truncated]..." if len(snippet) > 3000 else "")
        console.print(Syntax(preview, lang, theme="monokai", line_numbers=True, start_line=start_line, word_wrap=False))
        return {"success": True, "content": snippet, "total_lines": total, "path": str(p.resolve())}
    except Exception as e:
        return {"success": False, "error": str(e)}



def _tool_write_file(path: str, content: str, overwrite: bool = True) -> dict:
    console.print(f"[dim]write_file: [cyan]{rich_escape(path)}[/cyan][/dim]")
    try:
        p = _resolve_user_path(path)
        ok, err = _enforce_directory_access(p, f"write file {p}", for_write=True)
        if not ok:
            return {"success": False, "error": err}
        if p.exists() and not overwrite:
            return {"success": False, "error": "File exists. Set overwrite=true."}
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        console.print(f"[green]OK Written {p.stat().st_size} bytes -> {rich_escape(str(p.resolve()))}[/green]")
        return {"success": True, "path": str(p.resolve()), "size_bytes": p.stat().st_size}
    except Exception as e:
        return {"success": False, "error": str(e)}



def _tool_edit_file(path: str, old_str: str, new_str: str) -> dict:
    console.print(f"[dim]edit_file: [cyan]{rich_escape(path)}[/cyan][/dim]")
    try:
        p = _resolve_user_path(path)
        ok, err = _enforce_directory_access(p, f"edit file {p}", for_write=True)
        if not ok:
            return {"success": False, "error": err}
        if not p.exists():
            return {"success": False, "error": f"Not found: {path}"}
        original = p.read_text(encoding="utf-8", errors="replace")
        if old_str not in original:
            return {"success": False, "error": "old_str not found in file - no replacement made."}
        p.write_text(original.replace(old_str, new_str, 1), encoding="utf-8")
        console.print("[green]OK Edited successfully.[/green]")
        return {"success": True, "path": str(p.resolve())}
    except Exception as e:
        return {"success": False, "error": str(e)}



def _tool_list_directory(path: str = ".", pattern: str = "*") -> dict:
    target = path if path != "." else _cwd
    console.print(f"[dim]list_directory: [cyan]{rich_escape(target)}[/cyan][/dim]")
    try:
        p = _resolve_user_path(target)
        ok, err = _enforce_directory_access(p, f"list directory {p}", for_write=False)
        if not ok:
            return {"success": False, "error": err}
        if not p.exists():
            return {"success": False, "error": f"Not found: {path}"}
        entries = []
        for item in sorted(p.iterdir()):
            if not fnmatch.fnmatch(item.name, pattern):
                continue
            entries.append({"name": item.name,
                            "type": "directory" if item.is_dir() else "file",
                            **({"size_bytes": item.stat().st_size} if item.is_file() else {})})
        table = Table(title=f"Directory {rich_escape(str(p.resolve()))}", border_style="cyan")
        table.add_column("Name")
        table.add_column("Type", style="dim")
        table.add_column("Size", justify="right", style="green")
        for e in entries[:100]:
            table.add_row(e['name'], e['type'], str(e.get("size_bytes", "")))
        console.print(table)
        return {"success": True, "path": str(p.resolve()), "entries": entries}
    except Exception as e:
        return {"success": False, "error": str(e)}



def _tool_change_directory(path: str) -> dict:
    global _cwd
    console.print(f"[dim]change_directory: [cyan]{rich_escape(path)}[/cyan][/dim]")
    try:
        p = _resolve_user_path(path)
        if not p.exists():
            return {"success": False, "error": f"Not found: {path}"}
        if not p.is_dir():
            return {"success": False, "error": f"Not a directory: {path}"}
        ok, err = _enforce_directory_access(p, f"change directory to {p}", for_write=False)
        if not ok:
            return {"success": False, "error": err}
        _cwd = str(p)
        console.print(f"[green]OK cwd -> {rich_escape(_cwd)}[/green]")
        return {"success": True, "cwd": _cwd}
    except Exception as e:
        return {"success": False, "error": str(e)}



def _tool_search_files(path: str, pattern: str, content_search: str = "") -> dict:
    target_path = _resolve_user_path(path)
    target = str(target_path)
    console.print(f"[dim]search_files: [cyan]{rich_escape(target)}[/cyan] | [yellow]{rich_escape(pattern)}[/yellow][/dim]")
    try:
        ok, err = _enforce_directory_access(target_path, f"search files under {target_path}", for_write=False)
        if not ok:
            return {"success": False, "error": err}
        matches = []
        for m in target_path.rglob(pattern):
            if m.is_file():
                if content_search:
                    try:
                        if content_search.lower() in m.read_text(encoding="utf-8", errors="replace").lower():
                            matches.append(str(m.resolve()))
                    except Exception:
                        pass
                else:
                    matches.append(str(m.resolve()))
        matches = matches[:50]
        console.print(f"[green]Found {len(matches)} file(s).[/green]")
        for m in matches[:20]:
            console.print(f"  [cyan]{rich_escape(m)}[/cyan]")
        if len(matches) > 20:
            console.print(f"  [dim]+{len(matches)-20} more[/dim]")
        return {"success": True, "matches": matches, "count": len(matches)}
    except Exception as e:
        return {"success": False, "error": str(e)}





def _tool_delegate_task(task: str, model: str = "gemini-2.5-flash") -> dict:
    if not HANDOFF_ENABLED:
        return {"success": False, "error": "Handoff is disabled. The user must type /handoff to enable it."}
    
    console.print(Panel(f"Agent asking {model} to:\n[dim]{rich_escape(task)}[/dim]",
                        title=f"[bold magenta]🤖 Delegating to {model}[/bold magenta]",
                        border_style="magenta", padding=(0, 1)))
    
    client = GcliClient(auth=get_auth(), model_id=model)
    client._history.append({
        "role": "user",
        "parts": [{"text": f"You are a sub-agent executing a delegated task. Focus entirely on completing this task, and return a summary of results. The task is:\n\n{task}"}]
    })
    
    result_text = ""
    # We will block and use the standard stream, but NOT update the main live UI directly, 
    # since we want to print its progress independently.
    
    live = Live(auto_refresh=True, refresh_per_second=10)
    live.start()
    
    try:
        MAX_ROUNDS = 30
        for _ in range(MAX_ROUNDS):
            chunk_text = ""
            function_calls = []
            
            for text, fc in client._step_with_retry_stream(live):
                if text:
                    chunk_text += text
                    # We can show a miniaturized panel for the sub-agent
                    live.update(Panel(Markdown(chunk_text), title=f"[bold magenta]↳ Sub-agent ({model})[/bold magenta]", border_style="magenta", padding=(0,2)))
                if fc:
                    function_calls.append(fc)
            
            result_text += chunk_text + "\n"
            
            if not function_calls:
                break
                
            tool_response_parts = []
            model_parts = []
            if chunk_text: model_parts.append({"text": chunk_text})
            for raw_p in function_calls:
                model_parts.append(raw_p)
            client._history.append({"role": "model", "parts": model_parts})
                
            for raw_p in function_calls:
                fc = raw_p["functionCall"]
                fc_name = fc.get("name")
                fc_args = fc.get("args", {})
                _record_tool_call(fc_name or "unknown")
                if fc_name in TOOL_DISPATCH:
                    try:
                        res = TOOL_DISPATCH[fc_name](**fc_args)
                    except Exception as e:
                        res = {"success": False, "error": str(e)}
                else:
                    res = {"success": False, "error": f"Unknown tool: {fc_name}"}
                tool_response_parts.append({
                    "functionResponse": {"name": fc_name, "response": {"result": res}}
                })
            client._history.append({"role": "user", "parts": tool_response_parts})
    except Exception as e:
        live.stop()
        return {"success": False, "error": str(e)}
    
    live.stop()
    return {"success": True, "sub_agent_response": result_text}


def _tool_delete_file(path: str) -> dict:
    if APP_SETTINGS.get("safe_mode", True):
        return {"success": False, "error": "Blocked by safe_mode. Disable with /set safe_mode false."}
    console.print(f"[dim]delete_file: [cyan]{rich_escape(path)}[/cyan][/dim]")
    try:
        p = _resolve_user_path(path)
        ok, err = _enforce_directory_access(p, f"delete {p}", for_write=True)
        if not ok:
            return {"success": False, "error": err}
        if not p.exists():
            return {"success": False, "error": f"Not found: {path}"}
        if p.is_dir():
            shutil.rmtree(p)
            console.print(f"[green]OK Deleted directory: {rich_escape(str(p.resolve()))}[/green]")
        else:
            p.unlink()
            console.print(f"[green]OK Deleted file: {rich_escape(str(p.resolve()))}[/green]")
        return {"success": True, "path": str(p.resolve())}
    except Exception as e:
        return {"success": False, "error": str(e)}



def _tool_create_directory(path: str) -> dict:
    console.print(f"[dim]create_directory: [cyan]{rich_escape(path)}[/cyan][/dim]")
    try:
        p = _resolve_user_path(path)
        ok, err = _enforce_directory_access(p, f"create directory {p}", for_write=True)
        if not ok:
            return {"success": False, "error": err}
        p.mkdir(parents=True, exist_ok=True)
        console.print(f"[green]OK Created directory: {rich_escape(str(p.resolve()))}[/green]")
        return {"success": True, "path": str(p.resolve())}
    except Exception as e:
        return {"success": False, "error": str(e)}



def _tool_move_file(source: str, destination: str) -> dict:
    console.print(f"[dim]move_file: [cyan]{rich_escape(source)}[/cyan] -> [cyan]{rich_escape(destination)}[/cyan][/dim]")
    try:
        src = _resolve_user_path(source)
        dst = _resolve_user_path(destination)
        ok, err = _enforce_directory_access(src, f"move source {src}", for_write=True)
        if not ok:
            return {"success": False, "error": err}
        ok, err = _enforce_directory_access(dst, f"move destination {dst}", for_write=True)
        if not ok:
            return {"success": False, "error": err}
        if not src.exists():
            return {"success": False, "error": f"Source not found: {source}"}
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        console.print(f"[green]OK Moved to: {rich_escape(str(dst.resolve()))}[/green]")
        return {"success": True, "source": str(src.resolve()), "destination": str(dst.resolve())}
    except Exception as e:
        return {"success": False, "error": str(e)}



def _tool_read_url(url: str) -> dict:
    console.print(f"[dim]🌐 read_url: [cyan]{rich_escape(url)}[/cyan][/dim]")
    try:
        import urllib.request, re
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
        html = urllib.request.urlopen(req, timeout=15).read().decode('utf-8', errors='replace')
        
        text = re.sub(r'<style.*?>.*?</style>', '', html, flags=re.DOTALL|re.IGNORECASE)
        text = re.sub(r'<script.*?>.*?</script>', '', text, flags=re.DOTALL|re.IGNORECASE)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        
        if len(text) > 60000:
            text = text[:60000] + "\n...[TRUNCATED]"
        return {"success": True, "text": text}
    except Exception as e:
        return {"success": False, "error": str(e)}

def _tool_search_web(query: str, max_results: int = 5) -> dict:
    import warnings, contextlib, io, sys
    console.print(f"[dim]🌐 search_web: [cyan]{rich_escape(query)}[/cyan][/dim]")
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            console.print("[dim]  Installing 'ddgs' package for web search...[/dim]")
            subprocess.run([sys.executable, "-m", "pip", "install", "ddgs", "-q"], check=True)
            from ddgs import DDGS
            
        with warnings.catch_warnings(), contextlib.redirect_stderr(io.StringIO()):
            warnings.simplefilter('ignore')
            results = list(DDGS().text(query, max_results=max_results))
            
        console.print(f"[green]✅ Found {len(results)} results.[/green]")
        for r in results[:3]:
            console.print(f"  [dim]• {rich_escape(r.get('title', ''))}[/dim]")
        return {"success": True, "results": results}
    except Exception as e:
        return {"success": False, "error": str(e)}

TOOL_DISPATCH = {
    "run_command": _tool_run_command,
    "read_file": _tool_read_file,
    "write_file": _tool_write_file,
    "edit_file": _tool_edit_file,
    "list_directory": _tool_list_directory,
    "change_directory": _tool_change_directory,
    "search_files": _tool_search_files,
    "delegate_task": _tool_delegate_task,
    "delete_file": _tool_delete_file,
    "create_directory": _tool_create_directory,
    "move_file": _tool_move_file,
    "search_web": _tool_search_web,
    "read_url": _tool_read_url,
}


# ══════════════════════════════════════════════════════════════════════════════
#  MULTI-PROVIDER SUPPORT
# ══════════════════════════════════════════════════════════════════════════════

def _detect_provider(model_id: str) -> str:
    """Detect which AI provider a model ID belongs to."""
    mid = model_id.lower().strip()
    if mid.startswith(("gpt-", "o1-", "o3-", "o4-", "o1", "o3", "o4", "text-davinci", "chatgpt")):
        return "openai"
    elif mid.startswith("claude-"):
        return "anthropic"
    elif mid.startswith(("ollama/", "ollama:")):
        return "ollama"
    else:
        return "gemini"


def _load_provider_keys() -> dict:
    """Load saved API keys for all providers from ~/.gcli/providers.json."""
    try:
        if PROVIDERS_KEY_FILE.exists():
            return json.loads(PROVIDERS_KEY_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_provider_key(provider: str, key: str) -> None:
    """Save an API key for a provider to ~/.gcli/providers.json."""
    try:
        keys = _load_provider_keys()
        keys[provider] = key
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        PROVIDERS_KEY_FILE.write_text(json.dumps(keys, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        console.print(f"[yellow]Warning: could not save provider key: {e}[/yellow]")


def _get_or_prompt_provider_key(provider: str, model_id: str, allow_prompt: bool = True) -> str:
    """
    Get an API key for a provider. Checks env vars, saved file, then prompts.
    """
    # Check environment variables
    env_map = {
        "openai": ["OPENAI_API_KEY"],
        "anthropic": ["ANTHROPIC_API_KEY"],
        "ollama": [],  # No key needed for local Ollama
    }
    for env_var in env_map.get(provider, []):
        val = os.environ.get(env_var)
        if val:
            console.print(f"[green]✓[/green] [cyan]{env_var}[/cyan] found in environment.\n")
            return val

    # Ollama needs no key
    if provider == "ollama":
        return ""

    # Check saved keys
    saved = _load_provider_keys()
    if provider in saved and saved[provider].strip():
        console.print(f"[green]✓[/green] Loaded saved [bold]{provider}[/bold] API key.\n")
        return saved[provider].strip()

    if not allow_prompt:
        raise RuntimeError(
            f"No API key found for provider '{provider}'. "
            f"Set the appropriate env var or run interactively to save one."
        )

    # Interactive prompt
    provider_urls = {
        "openai": "https://platform.openai.com/api-keys",
        "anthropic": "https://console.anthropic.com/settings/keys",
    }
    url = provider_urls.get(provider, "")
    console.print(Panel(
        f"[bold cyan]{provider.title()} API Key Required[/bold cyan]\n\n"
        f"Model [bold]{rich_escape(model_id)}[/bold] uses the [bold]{provider.title()}[/bold] API.\n"
        + (f"\nGet a key at: [bold]{url}[/bold]" if url else ""),
        border_style="cyan", padding=(0, 1)
    ))
    key = Prompt.ask(f"[bold]Paste your {provider.title()} API key[/bold] (hidden)", password=True).strip()
    if not key:
        console.print("[red]No key entered. Exiting.[/red]")
        sys.exit(1)
    if Prompt.ask("[dim]Save key for future runs?[/dim]", choices=["y", "n"], default="y") == "y":
        _save_provider_key(provider, key)
        console.print(f"[green]✓ Saved to ~/.gcli/providers.json[/green]\n")
    return key


# ══════════════════════════════════════════════════════════════════════════════
#  GEMINI TOOL SCHEMA
# ══════════════════════════════════════════════════════════════════════════════

HANDOFF_ENABLED = False


def _get_tool_specs() -> list:
    """Return the canonical tool specs shared by all providers."""
    return [
        ("run_command", "Execute a PowerShell command on the Windows machine. Use for installs, git, compiling, scripts. IMPORTANT: For infinite servers/games set background=True to prevent hanging.", [
            ("command", "string", "PowerShell command or script to execute.", True),
            ("timeout", "integer", "Timeout in seconds. Default 120.", False),
            ("background", "boolean", "If True, runs command in an infinite detached terminal window and returns immediately.", False),
        ]),
        ("read_file", "Read file contents from disk. Supports optional line range.", [
            ("path", "string", "Absolute or relative file path.", True),
            ("start_line", "integer", "First line to read (1-indexed). Default 1.", False),
            ("end_line", "integer", "Last line inclusive. 0 = all.", False),
        ]),
        ("write_file", "Write or overwrite a file. Creates parent directories automatically.", [
            ("path", "string", "File path.", True),
            ("content", "string", "Full content to write.", True),
            ("overwrite", "boolean", "Overwrite if exists. Default true.", False),
        ]),
        ("edit_file", "Surgically replace the FIRST occurrence of old_str with new_str. Safer than write_file for small changes.", [
            ("path", "string", "File path.", True),
            ("old_str", "string", "Exact string to find.", True),
            ("new_str", "string", "Replacement string.", True),
        ]),
        ("list_directory", "List files and directories at a path.", [
            ("path", "string", "Directory to list.", True),
            ("pattern", "string", "Optional glob filter e.g. '*.py'.", False),
        ]),
        ("change_directory", "Change working directory.", [
            ("path", "string", "Directory to switch into.", True),
        ]),
                ("delegate_task", "Delegate a sub-task to be solved completely by another AI model. Useful for delegating complex, isolated analysis or creating scripts while you do something else. The sub-agent has the SAME tool access as you. Only available if handoff is enabled.", [
            ("task", "string", "A crystal clear description of exactly what the sub-agent should do.", True),
            ("model", "string", "The Gemini model ID to use for the sub-agent (e.g. gemini-2.5-flash)", False),
        ]),
                ("delete_file", "Delete a file or directory permanently.", [
            ("path", "string", "Absolute or relative path to delete.", True),
        ]),
        ("create_directory", "Create a new directory (and any necessary parent directories).", [
            ("path", "string", "Absolute or relative path to create.", True),
        ]),
        ("move_file", "Move or rename a file or directory.", [
            ("source", "string", "Path of the file to move.", True),
            ("destination", "string", "New path.", True),
        ]),
        ("search_web", "Search the internet for real-time information, documentation, news, or debugging help via DuckDuckGo.", [
            ("query", "string", "The search query to look up.", True),
            ("max_results", "integer", "Number of results to return (default 5, max 10).", False),
        ]),
        ("read_url", "Read the raw text content of a webpage (e.g. for reading documentation or GitHub gists).", [
            ("url", "string", "The HTTP/HTTPS URL to read.", True),
        ]),
        ("search_files", "Recursively search for files by glob pattern.", [
            ("path", "string", "Root search directory.", True),
            ("pattern", "string", "Glob pattern e.g. '*.py'.", True),
            ("content_search", "string", "Only return files containing this string.", False),
        ]),
    ]


def _build_bridge_tools() -> list:
    """Build the raw generic JSON dictionaries for the Node.js bridge."""
    specs = _get_tool_specs()
    type_map = {"string": "STRING", "integer": "INTEGER", "boolean": "BOOLEAN"}
    decls = []
    for name, desc, params in specs:
        props = {p[0]: {"type": type_map[p[1]], "description": p[2]} for p in params}
        required = [p[0] for p in params if p[3]]
        decls.append({
            "name": name, "description": desc,
            "parameters": {"type": "OBJECT", "properties": props, "required": required}
        })
    return [{"function_declarations": decls}]


def _build_tools() -> list:
    """Build the typed Gemini SDK tool objects."""
    specs = _get_tool_specs()
    type_map = {"string": gt.Type.STRING, "integer": gt.Type.INTEGER, "boolean": gt.Type.BOOLEAN}
    decls = []
    for name, desc, params in specs:
        props = {p[0]: gt.Schema(type=type_map[p[1]], description=p[2]) for p in params}
        required = [p[0] for p in params if p[3]]
        decls.append(gt.FunctionDeclaration(
            name=name, description=desc,
            parameters=gt.Schema(type=gt.Type.OBJECT, properties=props, required=required)
        ))
    return [gt.Tool(function_declarations=decls)]


def _build_openai_tools() -> list:
    """Build tool definitions in OpenAI function-calling format."""
    specs = _get_tool_specs()
    type_map = {"string": "string", "integer": "integer", "boolean": "boolean"}
    tools = []
    for name, desc, params in specs:
        props = {p[0]: {"type": type_map[p[1]], "description": p[2]} for p in params}
        required = [p[0] for p in params if p[3]]
        tools.append({
            "type": "function",
            "function": {
                "name": name,
                "description": desc,
                "parameters": {"type": "object", "properties": props, "required": required}
            }
        })
    return tools


def _build_anthropic_tools() -> list:
    """Build tool definitions in Anthropic tool_use format."""
    specs = _get_tool_specs()
    type_map = {"string": "string", "integer": "integer", "boolean": "boolean"}
    tools = []
    for name, desc, params in specs:
        props = {p[0]: {"type": type_map[p[1]], "description": p[2]} for p in params}
        required = [p[0] for p in params if p[3]]
        tools.append({
            "name": name,
            "description": desc,
            "input_schema": {"type": "object", "properties": props, "required": required}
        })
    return tools


def _history_to_openai(history: list, system_prompt: str) -> list:
    """Convert GCLI internal history to OpenAI messages format."""
    messages = [{"role": "system", "content": system_prompt}]
    for hi, h in enumerate(history):
        role = h["role"]
        parts = h.get("parts", [])

        if role == "user":
            texts = [p["text"] for p in parts if "text" in p]
            tool_results = [p for p in parts if "functionResponse" in p]
            if tool_results:
                for pi, p in enumerate(tool_results):
                    fr = p["functionResponse"]
                    call_id = fr.get("id") or f"call_{hi}_{pi}"
                    result = fr.get("response", {}).get("result", fr.get("response", {}))
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": json.dumps(result) if not isinstance(result, str) else result,
                    })
                if texts:
                    messages.append({"role": "user", "content": "\n".join(texts)})
            else:
                messages.append({"role": "user", "content": "\n".join(texts) or ""})

        elif role == "model":
            texts = [p["text"] for p in parts if "text" in p]
            func_calls = [p for p in parts if "functionCall" in p]
            msg: dict = {
                "role": "assistant",
                "content": "\n".join(texts) if texts else (None if func_calls else ""),
            }
            if func_calls:
                msg["tool_calls"] = [
                    {
                        "id": p["functionCall"].get("id") or f"call_{hi}_{pi}",
                        "type": "function",
                        "function": {
                            "name": p["functionCall"]["name"],
                            "arguments": json.dumps(p["functionCall"].get("args", {})),
                        },
                    }
                    for pi, p in enumerate(func_calls)
                ]
            messages.append(msg)

    return messages


def _history_to_anthropic(history: list) -> list:
    """Convert GCLI internal history to Anthropic messages format."""
    messages = []
    for hi, h in enumerate(history):
        role = h["role"]
        parts = h.get("parts", [])

        if role == "user":
            texts = [p["text"] for p in parts if "text" in p]
            tool_results = [p for p in parts if "functionResponse" in p]
            content: list = []
            for pi, p in enumerate(tool_results):
                fr = p["functionResponse"]
                tool_use_id = fr.get("id") or f"toolu_{hi}_{pi}"
                result = fr.get("response", {}).get("result", fr.get("response", {}))
                result_str = json.dumps(result) if not isinstance(result, str) else result
                content.append({"type": "tool_result", "tool_use_id": tool_use_id, "content": result_str})
            for t in texts:
                content.append({"type": "text", "text": t})
            if not content:
                content = [{"type": "text", "text": ""}]
            messages.append({"role": "user", "content": content})

        elif role == "model":
            texts = [p["text"] for p in parts if "text" in p]
            func_calls = [p for p in parts if "functionCall" in p]
            content = []
            for t in texts:
                content.append({"type": "text", "text": t})
            for pi, p in enumerate(func_calls):
                fc = p["functionCall"]
                tool_use_id = fc.get("id") or f"toolu_{hi}_{pi}"
                content.append({
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": fc["name"],
                    "input": fc.get("args", {}),
                })
            if not content:
                content = [{"type": "text", "text": ""}]
            messages.append({"role": "assistant", "content": content})

    return messages


# ══════════════════════════════════════════════════════════════════════════════
#  GEMINI CLI INTEGRATION
# ══════════════════════════════════════════════════════════════════════════════

def _get_gemini_cli_email() -> str:
    """Get the active account email from Gemini CLI."""
    try:
        if GEMINI_CLI_ACCOUNTS.exists():
            data = json.loads(GEMINI_CLI_ACCOUNTS.read_text(encoding="utf-8"))
            return data.get("active", "")
    except Exception:
        pass
    return ""


def _read_gemini_cli_api_key() -> str | None:
    """Read the API key the Gemini CLI stored in Windows Credential Manager.
    Runs get_gemini_key.mjs via Node.js — same loadApiKey() the gemini CLI uses.
    Returns the key string, or None if not found / Gemini CLI not installed.
    """
    if not _GEMINI_KEY_SCRIPT.exists():
        return None
    try:
        result = subprocess.run(
            ["node", str(_GEMINI_KEY_SCRIPT)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return None


# ══════════════════════════════════════════════════════════════════════════════
#  AUTH
# ══════════════════════════════════════════════════════════════════════════════


_BRIDGE_SCRIPT = Path(__file__).parent / "bridge.mjs"


def _try_oauth_bridge() -> dict | None:
    """Spawn bridge.mjs for OAuth auth check (no API key needed).
    Returns {"mode":"oauth",...} or {"mode":"oauth_missing"} or None.
    """
    if not _BRIDGE_SCRIPT.exists():
        return None
    try:
        import threading
        proc = subprocess.Popen(
            ["node", str(_BRIDGE_SCRIPT)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
            encoding="utf-8", errors="replace",
        )
        info = {}
        def _r():
            try: info.update(json.loads(proc.stdout.readline().strip()))
            except Exception: pass
        t = threading.Thread(target=_r, daemon=True); t.start(); t.join(timeout=15)
        proc.stdin.close()
        try: proc.wait(timeout=2)
        except Exception: pass
        if info.get("ready"):
            return {"mode": "oauth", "email": info.get("email",""), "tier": info.get("tier","")}
        if info.get("error") == "NO_OAUTH_CREDS":
            return {"mode": "oauth_missing"}
        return None
    except Exception:
        return None

def get_auth(allow_prompt: bool = True) -> dict:
    """
    Determine authentication. Returns {"mode": "apikey", "api_key": ...}.

    Priority:
      1. GEMINI_API_KEY / GOOGLE_API_KEY env var
      2. API key stored by Gemini CLI (Windows Credential Manager / encrypted file)
      3. Saved GCLI key  (~/.gcli/apikey.txt)
      4. Interactive menu
    """
    # 1. Env var
    env_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if env_key:
        console.print("[green]✓[/green] [cyan]GEMINI_API_KEY[/cyan] found in environment.\n")
        return {"mode": "apikey", "api_key": env_key}

    # 2. Gemini CLI OAuth (no API key needed!)
    with console.status("[cyan]Connecting to Gemini CLI...[/cyan]", spinner="dots"):
        oauth = _try_oauth_bridge()
    if oauth and oauth.get("mode") == "oauth":
        email, tier = oauth.get("email",""), oauth.get("tier","")
        console.print(Panel(
            f"[bold green]✓ Connected via Gemini CLI (no API key!)[/bold green]\n\n"
            f"  Account : [bold cyan]{rich_escape(email)}[/bold cyan]\n"
            f"  Plan    : [bold]{rich_escape(tier)}[/bold]",
            title="[bold green]🔗 Gemini CLI Integration[/bold green]",
            border_style="green", padding=(0, 1)
        ))
        return {"mode": "oauth", "email": email, "tier": tier}
    if oauth and oauth.get("mode") == "oauth_missing":
        console.print(Panel(
            "[yellow]Gemini CLI OAuth session has expired.\n"
            "Run [bold cyan]gemini[/bold cyan] once to sign in again,\n"
            "then restart GCLI (no API key needed!)\n\n"
            "Falling back to API key for now.[/yellow]",
            title="[yellow]ℹ Gemini CLI session expired[/yellow]",
            border_style="yellow", padding=(0, 1)
        ))

    # 3. Gemini CLI stored API key (fallback)
    gemini_key = _read_gemini_cli_api_key()
    if gemini_key:
        email2 = _get_gemini_cli_email()
        label = f" ([dim]{rich_escape(email2)}[/dim])" if email2 else ""
        console.print(Panel(
            f"[bold green]✓ Using Gemini CLI API key[/bold green]{label}\n\n"
            "  Loaded from Gemini CLI's credential store.",
            title="[bold green]🔗 Gemini CLI Integration[/bold green]",
            border_style="green", padding=(0, 1)
        ))
        return {"mode": "apikey", "api_key": gemini_key}

        # 3. Saved GCLI API key
    if SAVED_KEY_FILE.exists():
        key = SAVED_KEY_FILE.read_text(encoding="utf-8").strip()
        if key:
            console.print("[green]✓[/green] Loaded saved API key from [dim]~/.gcli/apikey.txt[/dim]\n")
            return {"mode": "apikey", "api_key": key}

    # 4. Interactive menu
    if not allow_prompt:
        raise RuntimeError(
            "No authentication available for non-interactive mode. "
            "Set GEMINI_API_KEY/GOOGLE_API_KEY, run `gemini login`, or save ~/.gcli/apikey.txt first."
        )

    table = Table(title="[bold]Connect to Gemini[/bold]", border_style="cyan",
                  show_header=False, padding=(0, 2))
    table.add_column("#", style="cyan bold", justify="right")
    table.add_column("Option", style="bold white")
    table.add_column("Notes", style="dim")
    table.add_row("1", "Quick Connect",
                  "Opens AI Studio in browser -> get free key -> paste here -> saved for future")
    table.add_row("2", "🔑 Enter API Key",
                  "Already have a key? Paste it directly")
    console.print(table)
    console.print()

    choice = Prompt.ask("[bold]Choose[/bold]", choices=["1", "2"], default="1")
    key = _quick_connect_flow() if choice == "1" else _manual_key_flow()
    return {"mode": "apikey", "api_key": key}


def _quick_connect_flow() -> str:
    """Open AI Studio in browser, let user copy key, save it."""
    console.print(Panel(
        "[bold cyan]Quick Connect[/bold cyan]\n\n"
        "[bold]Step 1[/bold] - Browser opens to Google AI Studio\n"
        "[bold]Step 2[/bold] - Sign in with your Google account\n"
        "[bold]Step 3[/bold] - Click [bold green]\"Create API key\"[/bold green] and copy it\n"
        "[bold]Step 4[/bold] - Paste it below\n\n"
        "[bold green]Free[/bold green]   "
        "[bold green]No credit card[/bold green]   "
        "[bold green]No Cloud Console[/bold green]   "
        "[bold green]No billing[/bold green]\n"
        "[dim]Key saved locally - future runs skip this step.[/dim]",
        border_style="cyan", padding=(0, 1)
    ))
    try:
        subprocess.Popen(
            ["powershell", "-Command", f"Start-Process '{AISTUDIO_URL}'"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        console.print(f"[dim]Browser opened: {AISTUDIO_URL}[/dim]")
    except Exception:
        console.print(f"[dim]Open manually: [bold]{AISTUDIO_URL}[/bold][/dim]")

    console.print()
    key = Prompt.ask("[bold]Paste your API key[/bold] (hidden)", password=True).strip()
    if not key:
        console.print("[red]No key entered. Exiting.[/red]")
        sys.exit(1)
    _save_key(key)
    console.print("[green]Key saved - future runs will be instant.[/green]\n")
    return key


def _manual_key_flow() -> str:
    """Prompt for manual key entry."""
    console.print(f"[dim]Don't have a key? Get one free at: [bold]{AISTUDIO_URL}[/bold][/dim]\n")
    key = Prompt.ask("[bold]Paste API key[/bold] (hidden)", password=True).strip()
    if not key:
        console.print("[red]No key entered. Exiting.[/red]")
        sys.exit(1)
    if Prompt.ask("[dim]Save key for future runs?[/dim]", choices=["y", "n"], default="y") == "y":
        _save_key(key)
        console.print("[green]✓ Saved to ~/.gcli/apikey.txt[/green]\n")
    return key


def _save_key(key: str):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    SAVED_KEY_FILE.write_text(key, encoding="utf-8")


# ══════════════════════════════════════════════════════════════════════════════
#  MODEL SELECTION
# ══════════════════════════════════════════════════════════════════════════════

def select_model(auth_mode: str, interactive: bool = True) -> str:
    # (key, model_id, provider_label, description)
    models = [
        # ── Google Gemini ─────────────────────────────────────────────────────
        ("1",  "gemini-2.5-pro",                      "Gemini",     "[bold yellow]Flagship[/bold yellow] — most capable, deep reasoning"),
        ("2",  "gemini-2.5-flash",                    "Gemini",     "[bold green]Recommended[/bold green] — fast & smart (default)"),
        ("3",  "gemini-2.5-flash-lite",               "Gemini",     "[cyan]Lightest[/cyan] — highest free-tier quota"),
        ("4",  "gemini-2.0-flash",                    "Gemini",     "[white]Stable[/white] — reliable, widely available"),
        ("5",  "gemini-2.0-flash-thinking-exp-01-21", "Gemini",     "[bold purple]Thinking[/bold purple] — reasoning mode"),
        # ── OpenAI ────────────────────────────────────────────────────────────
        ("6",  "gpt-4o",                              "OpenAI",     "[bold bright_blue]GPT-4o[/bold bright_blue] — flagship multimodal"),
        ("7",  "gpt-4o-mini",                         "OpenAI",     "[blue]GPT-4o mini[/blue] — fast & affordable"),
        ("8",  "o3-mini",                             "OpenAI",     "[bold blue]o3-mini[/bold blue] — reasoning model"),
        # ── Anthropic ─────────────────────────────────────────────────────────
        ("9",  "claude-sonnet-4-5",                   "Anthropic",  "[bold magenta]Claude Sonnet[/bold magenta] — balanced performance"),
        ("10", "claude-opus-4-5",                     "Anthropic",  "[bold bright_magenta]Claude Opus[/bold bright_magenta] — most capable Claude"),
        ("11", "claude-haiku-4-5-20251001",           "Anthropic",  "[magenta]Claude Haiku[/magenta] — fastest Claude"),
        # ── Ollama (local) ────────────────────────────────────────────────────
        ("12", "ollama/llama3.2",                     "Ollama",     "[dim]Local llama3.2[/dim] — requires Ollama running at localhost:11434"),
    ]
    key_to_model = {k: (mid, prov, desc) for k, mid, prov, desc in models}
    default_key = "2"
    default_model = "gemini-2.5-flash"

    if not interactive:
        console.print(
            f"[muted]Non-interactive mode: using default model [brand]{default_model}[/brand].[/muted]\n"
        )
        return default_model

    table = Table(title="[bold white]✨ GCLI Model Selection ✨[/bold white]", border_style="bright_blue",
                  show_header=True, header_style="bold cyan", padding=(0, 1))
    table.add_column("#", style="cyan bold", justify="right", no_wrap=True)
    table.add_column("Provider", style="dim", no_wrap=True)
    table.add_column("Model ID", style="bright_white", no_wrap=True)
    table.add_column("Notes")

    prev_prov = None
    for k, mid, prov, desc in models:
        if prov != prev_prov and prev_prov is not None:
            table.add_row("", "", "", "")
        prev_prov = prov
        if k == default_key:
            table.add_row(
                f"[bold bright_green]{k}[/bold bright_green]",
                f"[bold bright_green]{prov}[/bold bright_green]",
                f"[bold bright_green]{mid}[/bold bright_green]",
                desc,
            )
        else:
            table.add_row(k, prov, mid, desc)

    console.print()
    console.print(table)
    console.print("  [muted]↳ Or type any model ID directly (e.g. gpt-4o, claude-sonnet-4-5, ollama/mistral)[/muted]\n")

    try:
        choice = Prompt.ask("[accent]❯ Model choice[/accent]", default=default_key)
    except EOFError:
        console.print(f"[warn]No terminal input. Falling back to [brand]{default_model}[/brand].[/warn]\n")
        return default_model

    choice = choice.strip() or default_key
    if choice in key_to_model:
        mid, prov, desc = key_to_model[choice]
        console.print(f"[bright_green]✓[/bright_green] Selected [bold cyan]{mid}[/bold cyan] [dim]({prov})[/dim]\n")
        return mid

    console.print(f"[bright_green]✓[/bright_green] Custom model: [bold cyan]{rich_escape(choice)}[/bold cyan]\n")
    return choice

# ══════════════════════════════════════════════════════════════════════════════
#  MULTI-PROVIDER CLIENT
# ══════════════════════════════════════════════════════════════════════════════

class GcliClient:
    def __init__(self, auth: dict, model_id: str, allow_prompt: bool = True):
        self._auth = auth
        self._history: list = []
        self._bridge_proc = None
        self._sdk = None
        self._openai_client = None
        self._anthropic_client = None
        self._model_id = model_id
        self._provider = _detect_provider(model_id)
        self._provider_key = ""

        # Build tool definitions for all providers upfront
        self._tools = _build_tools()
        self._bridge_tools = _build_bridge_tools()
        self._openai_tools = _build_openai_tools()
        self._anthropic_tools = _build_anthropic_tools()

        self._init_provider(allow_prompt=allow_prompt)

    def _init_provider(self, allow_prompt: bool = True) -> None:
        """Initialize the SDK for the current provider."""
        provider = self._provider
        if provider == "gemini":
            if self._auth.get("mode") == "oauth":
                import threading
                proc = subprocess.Popen(
                    ["node", str(_BRIDGE_SCRIPT)],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, text=True, bufsize=1,
                    encoding="utf-8", errors="replace",
                )
                ready = {}
                def _r():
                    try: ready.update(json.loads(proc.stdout.readline()))
                    except Exception: pass
                t = threading.Thread(target=_r, daemon=True); t.start(); t.join(timeout=15)
                if not ready.get("ready"):
                    raise RuntimeError(f"Bridge failed: {ready}")
                self._bridge_proc = proc
                self._sdk = None
            else:
                self._sdk = genai.Client(api_key=self._auth["api_key"])
        elif provider == "openai":
            self._provider_key = _get_or_prompt_provider_key("openai", self._model_id, allow_prompt)
            try:
                import openai as _openai
                base_url = None
                self._openai_client = _openai.OpenAI(api_key=self._provider_key, base_url=base_url)
            except ImportError:
                console.print("[yellow]Installing 'openai' package...[/yellow]")
                subprocess.run([sys.executable, "-m", "pip", "install", "openai", "-q"], check=True)
                import openai as _openai
                self._openai_client = _openai.OpenAI(api_key=self._provider_key)
        elif provider == "anthropic":
            self._provider_key = _get_or_prompt_provider_key("anthropic", self._model_id, allow_prompt)
            try:
                import anthropic as _anthropic
                self._anthropic_client = _anthropic.Anthropic(api_key=self._provider_key)
            except ImportError:
                console.print("[yellow]Installing 'anthropic' package...[/yellow]")
                subprocess.run([sys.executable, "-m", "pip", "install", "anthropic", "-q"], check=True)
                import anthropic as _anthropic
                self._anthropic_client = _anthropic.Anthropic(api_key=self._provider_key)
        elif provider == "ollama":
            # Ollama uses OpenAI-compatible API at localhost
            try:
                import openai as _openai
                self._openai_client = _openai.OpenAI(
                    api_key="ollama",
                    base_url="http://localhost:11434/v1",
                )
            except ImportError:
                console.print("[yellow]Installing 'openai' package for Ollama...[/yellow]")
                subprocess.run([sys.executable, "-m", "pip", "install", "openai", "-q"], check=True)
                import openai as _openai
                self._openai_client = _openai.OpenAI(api_key="ollama", base_url="http://localhost:11434/v1")

    def switch_model(self, new_model_id: str, allow_prompt: bool = True) -> None:
        """Switch to a different model, re-initializing the provider if needed."""
        new_provider = _detect_provider(new_model_id)
        self._model_id = new_model_id
        if new_provider != self._provider:
            self._provider = new_provider
            self._bridge_proc = None
            self._sdk = None
            self._openai_client = None
            self._anthropic_client = None
            self._init_provider(allow_prompt=allow_prompt)
            console.print(f"[green]✓[/green] Switched provider to [bold]{new_provider}[/bold]")

    def _bridge_generate_stream(self):
        """Send history to bridge.mjs, yield (text_chunk, func_call)."""
        import threading, uuid
        mid = str(uuid.uuid4())[:8]
        contents = []
        for h in self._history:
            parts = []
            for p in h["parts"]:
                if "text" in p: parts.append({"text": p["text"]})
                elif "functionCall" in p:
                    # Append the whole part which contains functionCall AND thoughtSignature/thought
                    parts.append(p)
                elif "functionResponse" in p: parts.append({"functionResponse": p["functionResponse"]})
            contents.append({"role": h["role"], "parts": parts})
            
        req = json.dumps({"id": mid, "method": "generate",
                          "model": self._model_id, "contents": contents,
                          "systemPrompt": SYSTEM_PROMPT,
                          "tools": self._bridge_tools}) + "\n"
                          
        self._bridge_proc.stdin.write(req)
        self._bridge_proc.stdin.flush()
        
        while True:
            line = self._bridge_proc.stdout.readline()
            if not line: break
            try:
                resp = json.loads(line.strip())
                if resp.get("error"): raise RuntimeError(resp["error"])
                if resp.get("id") and resp["id"] != mid: continue
                if resp.get("done"): break
                p = resp.get("part")
                if p:
                    if "text" in p: yield p["text"], None
                    if "functionCall" in p:
                        yield None, p  # yield the full raw dictionary (including thoughtSignature)
            except Exception as e:
                if isinstance(e, RuntimeError): raise
                pass

    def _step_stream(self):
        contents = []
        for h in self._history:
            parts = []
            for p in h["parts"]:
                if "text" in p:
                    parts.append(gt.Part(text=p["text"]))
                elif "functionCall" in p:
                    fc_kwargs = {"name": p["functionCall"]["name"], "args": p["functionCall"]["args"]}
                    # Normally thinking/signatures don't strictly apply through the SDK quite yet, but if they do:
                    part_kwargs = {"function_call": gt.FunctionCall(**fc_kwargs)}
                    if "thought" in p: part_kwargs["thought"] = p["thought"]
                    # thought_signature not strictly in standard SDK Part kwargs yet, but if it is we add it
                    try:
                        if "thoughtSignature" in p: part_kwargs["thought_signature"] = p["thoughtSignature"]
                    except: pass
                    
                    # Instead of fighting SDK kwargs, we just pass the basic ones. Code Assist JSON needs thoughtSignature
                    # but the standard genai SDK drops it anyway so we just do our best.
                    try:
                        parts.append(gt.Part(**part_kwargs))
                    except TypeError:
                        # Fallback if SDK doesn't support thought/thought_signature
                        parts.append(gt.Part(function_call=gt.FunctionCall(**fc_kwargs)))
                elif "functionResponse" in p:
                    parts.append(gt.Part(function_response=gt.FunctionResponse(
                        name=p["functionResponse"]["name"],
                        response=p["functionResponse"]["response"])))
            contents.append(gt.Content(role=h["role"], parts=parts))

        responseStream = self._sdk.models.generate_content_stream(
            model=self._model_id,
            contents=contents,
            config=gt.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                tools=self._tools,
                tool_config=gt.ToolConfig(
                    function_calling_config=gt.FunctionCallingConfig(mode="AUTO")
                ),
                temperature=float(APP_SETTINGS.get("temperature", 0.3)),
            )
        )
        
        for chunk in responseStream:
            if not chunk.candidates: continue
            if not chunk.candidates[0].content or getattr(chunk.candidates[0].content, 'parts', None) is None: continue
            for part in chunk.candidates[0].content.parts:
                if hasattr(part, "text") and part.text:
                    yield part.text, None
                if hasattr(part, "function_call") and part.function_call and part.function_call.name:
                    fc = part.function_call
                    raw_p = {"functionCall": {"name": fc.name, "args": dict(fc.args) if fc.args else {}}}
                    # Copy thought fields if they exist (rare via SDK but safe to try)
                    if hasattr(part, "thought"): raw_p["thought"] = part.thought
                    if hasattr(part, "thought_signature"): raw_p["thoughtSignature"] = part.thought_signature
                    yield None, raw_p

    def _step_stream_openai(self):
        """Stream a response from OpenAI (or Ollama's OpenAI-compatible API)."""
        real_model = self._model_id.replace("ollama/", "").replace("ollama:", "")
        messages = _history_to_openai(self._history, SYSTEM_PROMPT)
        tool_call_accum: dict = {}  # index -> {id, name, args_str}

        stream = self._openai_client.chat.completions.create(
            model=real_model,
            messages=messages,
            tools=self._openai_tools if self._provider != "ollama" else None,
            tool_choice="auto" if self._provider != "ollama" else None,
            stream=True,
            temperature=float(APP_SETTINGS.get("temperature", 0.3)),
        )
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta.content:
                yield delta.content, None
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in tool_call_accum:
                        tool_call_accum[idx] = {"id": "", "name": "", "args_str": ""}
                    if tc.id:
                        tool_call_accum[idx]["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            tool_call_accum[idx]["name"] += tc.function.name
                        if tc.function.arguments:
                            tool_call_accum[idx]["args_str"] += tc.function.arguments

        for idx in sorted(tool_call_accum.keys()):
            tc = tool_call_accum[idx]
            try:
                args = json.loads(tc["args_str"]) if tc["args_str"] else {}
            except Exception:
                args = {}
            yield None, {"functionCall": {"name": tc["name"], "args": args, "id": tc["id"]}}

    def _step_stream_anthropic(self):
        """Stream a response from Anthropic."""
        messages = _history_to_anthropic(self._history)
        current_tool: dict | None = None

        with self._anthropic_client.messages.stream(
            model=self._model_id,
            max_tokens=8192,
            system=SYSTEM_PROMPT,
            messages=messages,
            tools=self._anthropic_tools,
            temperature=float(APP_SETTINGS.get("temperature", 0.3)),
        ) as stream:
            for event in stream:
                etype = getattr(event, "type", None)
                if etype == "content_block_start":
                    block = event.content_block
                    if getattr(block, "type", None) == "tool_use":
                        current_tool = {"id": block.id, "name": block.name, "args_str": ""}
                elif etype == "content_block_delta":
                    delta = event.delta
                    dtype = getattr(delta, "type", None)
                    if dtype == "text_delta":
                        yield getattr(delta, "text", ""), None
                    elif dtype == "input_json_delta" and current_tool is not None:
                        current_tool["args_str"] += getattr(delta, "partial_json", "")
                elif etype == "content_block_stop":
                    if current_tool is not None:
                        try:
                            args = json.loads(current_tool["args_str"]) if current_tool["args_str"] else {}
                        except Exception:
                            args = {}
                        yield None, {"functionCall": {
                            "name": current_tool["name"],
                            "args": args,
                            "id": current_tool["id"],
                        }}
                        current_tool = None

    def _pick_stream_gen(self):
        """Pick the correct streaming generator for the current provider."""
        if self._provider == "gemini":
            if self._auth.get("mode") == "oauth":
                return self._bridge_generate_stream()
            return self._step_stream()
        elif self._provider in ("openai", "ollama"):
            return self._step_stream_openai()
        elif self._provider == "anthropic":
            return self._step_stream_anthropic()
        raise RuntimeError(f"Unknown provider: {self._provider}")

    def _step_with_retry_stream(self, live, max_retries: int = 6):
        for attempt in range(max_retries):
            try:
                for text, fc in self._pick_stream_gen():
                    yield text, fc
                return
            except Exception as e:
                err = str(e)
                err_low = err.lower()

                # Detect rate-limit / quota errors across providers
                is_rate_limit = any(kw in err for kw in (
                    "RESOURCE_EXHAUSTED", "429", "RateLimitError", "rate_limit_error",
                    "overloaded", "529",
                )) or "quota" in err_low or "rate limit" in err_low or "too many requests" in err_low

                if not is_rate_limit or attempt >= max_retries - 1:
                    raise

                # Calculate wait time with exponential backoff + jitter
                # 1. Try to extract a server-provided retry delay
                wait_sec = 0
                m = re.search(r"'retryDelay':\s*'(\d+)s'", err) or re.search(r"retry.after[\"']?\s*:\s*(\d+)", err_low)
                if m:
                    wait_sec = int(m.group(1)) + 1
                # 2. Check Retry-After style messages
                if not wait_sec:
                    m2 = re.search(r"retry after (\d+)", err_low)
                    if m2:
                        wait_sec = int(m2.group(1)) + 1
                # 3. Exponential backoff with jitter
                if not wait_sec:
                    base = min(60, 5 * (2 ** attempt))
                    wait_sec = int(base + random.uniform(0, base * 0.3))

                if live.is_started:
                    live.stop()

                console.print(Panel(
                    f"[yellow]Rate limited — waiting [bold]{wait_sec}s[/bold] then retrying "
                    f"[dim]({attempt + 1}/{max_retries - 1})[/dim][/yellow]",
                    title="[yellow]⏳ Rate Limited[/yellow]", border_style="yellow", padding=(0, 1)
                ))
                # Countdown display
                for remaining in range(wait_sec, 0, -1):
                    console.print(f"\r[dim]  retrying in {remaining}s...[/dim]  ", end="")
                    time.sleep(1)
                console.print()

    def send(self, user_text: str) -> None:
        self._history.append({"role": "user", "parts": [{"text": user_text}]})
        MAX_ROUNDS = int(APP_SETTINGS.get("max_rounds", 150))
        for _round in range(MAX_ROUNDS):
            console.print()

            accumulated_text = ""
            function_calls = []

            # transient=True for pure-tool-call rounds (spinner disappears cleanly),
            # but we switch the live display to persist once text starts flowing.
            live = Live(auto_refresh=True, refresh_per_second=15, transient=True)
            has_text = False
            try:
                live.start()
                live.update(Spinner("dots", text=Text.from_markup(" [dim]thinking...[/dim]")))
                for text_chunk, fc in self._step_with_retry_stream(live):
                    if text_chunk:
                        accumulated_text += text_chunk
                        if not has_text:
                            # First text chunk — switch to a non-transient live so the
                            # final frame persists (no double-print, no flash).
                            live.stop()
                            live = Live(auto_refresh=True, refresh_per_second=15, transient=False)
                            live.start()
                            has_text = True
                        live.update(Panel(Markdown(accumulated_text), title="[bold cyan]assistant[/bold cyan]", border_style="cyan", padding=(0, 1)))
                    if fc:
                        function_calls.append(fc)
                if live.is_started:
                    live.stop()
                # With transient=False the final panel frame already persists — no reprint needed.

            except Exception as e:
                if self._history and self._history[-1]["role"] == "user":
                    self._history.pop()
                if live.is_started:
                    live.stop()
                raise
                
            model_parts = []
            if accumulated_text: model_parts.append({"text": accumulated_text})
            for raw_p in function_calls:
                model_parts.append(raw_p)
            if model_parts:
                self._history.append({"role": "model", "parts": model_parts})

            if not function_calls:
                break
            tool_response_parts = []
            for raw_p in function_calls:
                fc = raw_p["functionCall"]
                fc_name = fc.get("name")
                fc_args = fc.get("args", {})
                _record_tool_call(fc_name or "unknown")
                if fc_name in TOOL_DISPATCH:
                    try:
                        if fc_name == "delegate_task":
                            # delegate_task manages its own Live context
                            result = TOOL_DISPATCH[fc_name](**fc_args)
                        else:
                            with console.status(
                                f"[cyan]{fc_name}[/cyan]",
                                spinner="dots",
                                spinner_style="cyan",
                            ):
                                result = TOOL_DISPATCH[fc_name](**fc_args)
                    except TypeError as e:
                        result = {"success": False, "error": f"Bad args: {e}"}
                else:
                    result = {"success": False, "error": f"Unknown tool: {fc_name}"}
                # Preserve the tool call ID for OpenAI/Anthropic round-trips
                fr_entry: dict = {"name": fc_name, "response": {"result": result}}
                call_id = fc.get("id")
                if call_id:
                    fr_entry["id"] = call_id
                tool_response_parts.append({
                    "functionResponse": fr_entry
                })
            self._history.append({"role": "user", "parts": tool_response_parts})
            console.print(Rule(style="dim cyan"))
        else:
            console.print(f"[dim]⚠️  Reached max tool rounds ({MAX_ROUNDS}).[/dim]")
# ══════════════════════════════════════════════════════════════════════════════
#  UI
# ══════════════════════════════════════════════════════════════════════════════

def _print_tool_history(client, limit: int | None = None) -> None:
    rows = []
    for h in client._history:
        if h.get("role") == "model":
            for p in h.get("parts", []):
                if "functionCall" in p:
                    fc = p["functionCall"]
                    rows.append(f"[bold magenta]AI Call:[/bold magenta] [cyan]{fc.get('name')}[/cyan] (args: {fc.get('args', {})})")
        elif h.get("role") == "user":
            for p in h.get("parts", []):
                if "functionResponse" in p:
                    fr = p["functionResponse"]
                    res = str(fr.get("response", {}).get("result", ""))[: APP_SETTINGS.get("history_preview_chars", 1000)]
                    rows.append(f"[bold green]Result:[/bold green] [dim]{rich_escape(res)}[/dim]")
    if limit:
        rows = rows[-limit:]
    console.print(Panel("[bold cyan]Tool Execution Log[/bold cyan]", border_style="cyan"))
    if not rows:
        console.print("[dim](no tool events yet)[/dim]")
    for row in rows:
        console.print(row)
    console.print()


def _print_runtime_stats(client) -> None:
    table = Table(title="Session Stats", border_style="cyan")
    table.add_column("Metric", style="bold cyan")
    table.add_column("Value", style="white")
    table.add_row("session_id", RUNTIME_STATE["session_id"])
    table.add_row("model", client._model_id)
    table.add_row("cwd", _cwd)
    table.add_row("uptime", _format_uptime(time.time() - RUNTIME_STATE["started_at"]))
    table.add_row("prompts", str(RUNTIME_STATE["prompt_count"]))
    table.add_row("tool_calls", str(RUNTIME_STATE["tool_call_count"]))
    table.add_row("safe_mode", str(APP_SETTINGS.get("safe_mode", True)))
    table.add_row("auto_save_session", str(APP_SETTINGS.get("auto_save_session", True)))
    table.add_row("temperature", str(APP_SETTINGS.get("temperature", 0.3)))
    table.add_row("max_rounds", str(APP_SETTINGS.get("max_rounds", 150)))
    console.print(table)


def _save_transcript(client, path: str) -> Path:
    p = Path(path) if Path(path).is_absolute() else Path(_cwd) / path
    lines = []
    for item in client._history:
        lines.append(f"## {item.get('role', 'unknown').upper()}")
        for part in item.get("parts", []):
            if "text" in part:
                lines.append(part["text"])
            elif "functionCall" in part:
                fc = part["functionCall"]
                lines.append(f"[functionCall] {fc.get('name')} args={fc.get('args', {})}")
            elif "functionResponse" in part:
                fr = part["functionResponse"]
                lines.append(f"[functionResponse] {fr.get('name')} result={fr.get('response', {})}")
        lines.append("")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def _scan_workspace(path: str) -> dict:
    target = Path(path) if Path(path).is_absolute() else Path(_cwd) / path
    target = target.resolve()
    files = 0
    dirs = 0
    total_size = 0
    by_ext = {}
    for root, dirnames, filenames in os.walk(target):
        dirs += len(dirnames)
        for name in filenames:
            files += 1
            full = Path(root) / name
            try:
                size = full.stat().st_size
            except Exception:
                size = 0
            total_size += size
            ext = full.suffix.lower() or "<none>"
            by_ext[ext] = by_ext.get(ext, 0) + 1
    return {
        "path": str(target),
        "files": files,
        "dirs": dirs,
        "total_size": total_size,
        "extensions": sorted(by_ext.items(), key=lambda x: x[1], reverse=True)[:15],
    }


def _send_prompt(client, prompt: str) -> None:
    _capture_undo_snapshot(client, prompt[:80])
    RUNTIME_STATE["last_prompt"] = prompt
    client.send(prompt)
    RUNTIME_STATE["prompt_count"] += 1
    RUNTIME_STATE["last_response"] = _extract_last_model_text(client._history)
    _autosave_session(client)
    if APP_SETTINGS.get("show_stats_after_response", False):
        _print_runtime_stats(client)


def _handle_advanced_command(user_input: str, client, auth: dict, macro_depth: int = 0) -> bool:
    global _cwd, HANDOFF_ENABLED
    text = user_input.strip()
    if not text.startswith("/"):
        return False
    parts = text.split(" ", 2)
    cmd = parts[0].lower()
    arg1 = parts[1].strip() if len(parts) > 1 else ""
    arg2 = parts[2].strip() if len(parts) > 2 else ""

    if cmd == "/version":
        console.print(f"[cyan]GCLI version:[/cyan] [bold]{APP_VERSION}[/bold]")
        return True
    if cmd == "/time":
        console.print(datetime.now().strftime("%H:%M:%S"))
        return True
    if cmd == "/date":
        console.print(datetime.now().strftime("%Y-%m-%d"))
        return True
    if cmd == "/session-id":
        console.print(RUNTIME_STATE["session_id"])
        return True
    if cmd == "/uptime":
        console.print(_format_uptime(time.time() - RUNTIME_STATE["started_at"]))
        return True
    if cmd == "/status":
        perms = _perm_state()
        console.print(Panel(
            f"[bold]Model:[/bold] {client._model_id}\n"
            f"[bold]cwd:[/bold] {rich_escape(_cwd)}\n"
            f"[bold]session_id:[/bold] {RUNTIME_STATE['session_id']}\n"
            f"[bold]uptime:[/bold] {_format_uptime(time.time() - RUNTIME_STATE['started_at'])}\n"
            f"[bold]prompts:[/bold] {RUNTIME_STATE['prompt_count']}\n"
            f"[bold]tool calls:[/bold] {RUNTIME_STATE['tool_call_count']}\n"
            f"[bold]safe_mode:[/bold] {APP_SETTINGS.get('safe_mode', True)}\n"
            f"[bold]auto_save_session:[/bold] {APP_SETTINGS.get('auto_save_session', True)}\n"
            f"[bold]perm_mode:[/bold] {perms.get('mode', 'prompt')}\n"
            f"[bold]trusted_roots:[/bold] {len(perms.get('trusted_roots', []))}\n"
            f"[bold]handoff:[/bold] {HANDOFF_ENABLED}",
            title="[bold cyan]GCLI Status[/bold cyan]", border_style="cyan"
        ))
        return True
    if cmd == "/stats":
        _print_runtime_stats(client)
        if RUNTIME_STATE["tool_calls"]:
            ranked = sorted(RUNTIME_STATE["tool_calls"].items(), key=lambda x: x[1], reverse=True)[:8]
            table = Table(title="Top Tools", border_style="magenta")
            table.add_column("Tool", style="bold magenta")
            table.add_column("Calls", justify="right", style="green")
            for n, c in ranked:
                table.add_row(n, str(c))
            console.print(table)
        return True
    if cmd == "/echo":
        console.print(arg1 + (" " + arg2 if arg2 else ""))
        return True
    if cmd == "/pwd":
        console.print(f"[cyan]cwd: {rich_escape(_cwd)}[/cyan]")
        return True
    if cmd == "/cd" and arg1:
        res = _tool_change_directory(arg1 + (" " + arg2 if arg2 else ""))
        if not res.get("success"):
            console.print(f"[red]{rich_escape(str(res.get('error')))}[/red]")
        return True
    if cmd == "/ls":
        pattern = arg1 if arg1 else "*"
        _tool_list_directory(".", pattern=pattern)
        return True
    if cmd == "/tree":
        target = arg1 if arg1 else "."
        _tool_run_command(f'tree "{target}" /A /F', timeout=120)
        return True
    if cmd == "/history" and arg1:
        try:
            n = max(1, int(arg1))
        except ValueError:
            n = 40
        _print_tool_history(client, limit=n)
        return True
    if cmd == "/last":
        if RUNTIME_STATE["last_response"]:
            console.print(Panel(Markdown(RUNTIME_STATE["last_response"]), title="[bold magenta]Last Response[/bold magenta]", border_style="magenta"))
        else:
            console.print("[dim]No previous response.[/dim]")
        return True
    if cmd == "/retry":
        if not RUNTIME_STATE["last_prompt"]:
            console.print("[dim]No previous prompt.[/dim]")
            return True
        _send_prompt(client, RUNTIME_STATE["last_prompt"])
        return True
    if cmd == "/replay":
        history = RUNTIME_STATE["shell_history"]
        if not history:
            console.print("[dim]No shell history yet.[/dim]")
            return True
        idx = -1
        if arg1 and arg1.lower() != "last":
            try:
                idx = int(arg1)
            except ValueError:
                idx = -1
        try:
            cmd_text = history[idx]["command"]
        except Exception:
            console.print("[red]Invalid replay index.[/red]")
            return True
        console.print(f"[dim]Replaying:[/dim] [yellow]{rich_escape(cmd_text)}[/yellow]")
        _tool_run_command(cmd_text)
        return True
    if cmd == "/clear-tools":
        RUNTIME_STATE["tool_calls"] = {}
        RUNTIME_STATE["tool_call_count"] = 0
        RUNTIME_STATE["shell_history"] = []
        console.print("[green]Cleared tool/shell statistics.[/green]")
        return True
    if cmd == "/settings":
        t = Table(title="Settings", border_style="cyan")
        t.add_column("Key", style="cyan")
        t.add_column("Value", style="white")
        for k in sorted(APP_SETTINGS.keys()):
            t.add_row(k, str(APP_SETTINGS[k]))
        console.print(t)
        return True
    if cmd == "/perm":
        perms = _perm_state()
        if arg1 in ("", "list"):
            t = Table(title="Directory Permissions", border_style="yellow")
            t.add_column("Mode", style="yellow")
            t.add_column("Trusted Roots", style="cyan")
            t.add_column("Allow Once", style="white")
            roots = "\n".join(perms.get("trusted_roots", [])) or "(none)"
            once = "\n".join(perms.get("allow_once", [])) or "(none)"
            t.add_row(str(perms.get("mode", "prompt")), roots, once)
            console.print(t)
            return True
        if arg1 == "mode" and arg2:
            mode = arg2.strip().lower()
            if mode not in ("prompt", "allow-all"):
                console.print("[red]Usage: /perm mode <prompt|allow-all>[/red]")
                return True
            perms["mode"] = mode
            _save_persistent_state()
            console.print(f"[green]Permission mode set to {mode}.[/green]")
            return True
        if arg1 == "trust" and arg2:
            p = str(_resolve_user_path(arg2))
            perms["trusted_roots"] = sorted(set(perms.get("trusted_roots", []) + [p]))
            _save_persistent_state()
            console.print(f"[green]Trusted root added:[/green] {rich_escape(p)}")
            return True
        if arg1 == "untrust" and arg2:
            p = str(_resolve_user_path(arg2))
            roots = perms.get("trusted_roots", [])
            if p in roots:
                roots.remove(p)
                _save_persistent_state()
                console.print(f"[green]Trusted root removed:[/green] {rich_escape(p)}")
            else:
                console.print("[red]Root not found in trusted list.[/red]")
            return True
        if arg1 == "once" and arg2:
            p = str(_permission_scope_for(_resolve_user_path(arg2), for_write=True))
            perms["allow_once"] = perms.get("allow_once", []) + [p]
            perms["allow_once"] = perms["allow_once"][-200:]
            console.print(f"[green]Allow-once added:[/green] {rich_escape(p)}")
            return True
        if arg1 == "clear-once":
            perms["allow_once"] = []
            console.print("[green]Cleared allow-once paths.[/green]")
            return True
        if arg1 == "check" and arg2:
            p = _permission_scope_for(_resolve_user_path(arg2), for_write=True)
            status = "allowed" if _has_directory_access(p) else "blocked"
            color = "green" if status == "allowed" else "red"
            console.print(f"[{color}]{status}[/{color}] {rich_escape(str(p))}")
            return True
        console.print("[red]Usage: /perm list|mode|trust|untrust|once|clear-once|check[/red]")
        return True
    if cmd == "/set" and arg1 and arg2:
        key = arg1
        try:
            APP_SETTINGS[key] = _coerce_setting(key, arg2)
            PERSISTENT_STATE["settings"][key] = APP_SETTINGS[key]
            _save_persistent_state()
            console.print(f"[green]Set {key} = {APP_SETTINGS[key]}[/green]")
        except Exception as e:
            console.print(f"[red]{rich_escape(str(e))}[/red]")
        return True
    if cmd == "/get" and arg1:
        if arg1 in APP_SETTINGS:
            console.print(f"[cyan]{arg1}[/cyan] = [bold]{APP_SETTINGS[arg1]}[/bold]")
        else:
            console.print("[red]Unknown setting[/red]")
        return True
    if cmd == "/reset-setting" and arg1:
        if arg1 in DEFAULT_PERSISTENT_STATE["settings"]:
            APP_SETTINGS[arg1] = DEFAULT_PERSISTENT_STATE["settings"][arg1]
            PERSISTENT_STATE["settings"][arg1] = APP_SETTINGS[arg1]
            _save_persistent_state()
            console.print(f"[green]Reset {arg1}.[/green]")
        else:
            console.print("[red]Unknown setting[/red]")
        return True
    if cmd == "/alias":
        aliases = PERSISTENT_STATE["aliases"]
        if arg1 in ("list", ""):
            if not aliases:
                console.print("[dim]No aliases.[/dim]")
            else:
                t = Table(title="Aliases", border_style="cyan")
                t.add_column("Name", style="cyan")
                t.add_column("Expansion", style="white")
                for k in sorted(aliases):
                    t.add_row(k, aliases[k])
                console.print(t)
            return True
        if arg1 == "add" and arg2:
            p2 = arg2.split(" ", 1)
            if len(p2) < 2:
                console.print("[red]Usage: /alias add <name> <expansion>[/red]")
                return True
            name = p2[0].strip().lstrip("/")
            expansion = p2[1].strip()
            aliases[name] = expansion
            _save_persistent_state()
            console.print(f"[green]Alias saved:[/green] {name} -> {rich_escape(expansion)}")
            return True
        if arg1 == "del" and arg2:
            name = arg2.split(" ", 1)[0].strip().lstrip("/")
            if name in aliases:
                aliases.pop(name)
                _save_persistent_state()
                console.print(f"[green]Alias removed:[/green] {name}")
            else:
                console.print("[red]Alias not found.[/red]")
            return True
        console.print("[red]Usage: /alias list | /alias add <name> <expansion> | /alias del <name>[/red]")
        return True
    if cmd == "/snippet":
        snippets = PERSISTENT_STATE["snippets"]
        if arg1 in ("list", ""):
            if not snippets:
                console.print("[dim]No snippets.[/dim]")
            else:
                t = Table(title="Snippets", border_style="cyan")
                t.add_column("Name", style="cyan")
                t.add_column("Preview", style="white")
                for k in sorted(snippets):
                    t.add_row(k, snippets[k].replace("\n", " ")[:80])
                console.print(t)
            return True
        if arg1 == "add" and arg2:
            p2 = arg2.split(" ", 1)
            if len(p2) < 2:
                console.print("[red]Usage: /snippet add <name> <text>[/red]")
                return True
            name, body = p2[0], p2[1]
            snippets[name] = body
            _save_persistent_state()
            console.print(f"[green]Snippet saved:[/green] {name}")
            return True
        if arg1 == "show" and arg2:
            name = arg2.split(" ", 1)[0]
            if name in snippets:
                console.print(Panel(Markdown(snippets[name]), title=f"Snippet: {name}", border_style="cyan"))
            else:
                console.print("[red]Snippet not found.[/red]")
            return True
        if arg1 == "del" and arg2:
            name = arg2.split(" ", 1)[0]
            if name in snippets:
                snippets.pop(name)
                _save_persistent_state()
                console.print(f"[green]Snippet removed:[/green] {name}")
            else:
                console.print("[red]Snippet not found.[/red]")
            return True
        if arg1 == "run" and arg2:
            p2 = arg2.split(" ")
            name = p2[0]
            if name not in snippets:
                console.print("[red]Snippet not found.[/red]")
                return True
            kv = {}
            for tok in p2[1:]:
                if "=" in tok:
                    k, v = tok.split("=", 1)
                    kv[k] = v
            rendered = _render_snippet(snippets[name], kv)
            _send_prompt(client, rendered)
            return True
        console.print("[red]Usage: /snippet list|add|show|del|run ...[/red]")
        return True
    if cmd == "/macro":
        macros = PERSISTENT_STATE["macros"]
        if arg1 in ("list", ""):
            if not macros:
                console.print("[dim]No macros.[/dim]")
            else:
                t = Table(title="Macros", border_style="cyan")
                t.add_column("Name", style="cyan")
                t.add_column("Steps", justify="right", style="green")
                for n, steps in sorted(macros.items()):
                    t.add_row(n, str(len(steps)))
                console.print(t)
            return True
        if arg1 == "add" and arg2:
            p2 = arg2.split(" ", 1)
            if len(p2) < 2:
                console.print("[red]Usage: /macro add <name> <step1 ;; step2 ...>[/red]")
                return True
            name, body = p2[0], p2[1]
            steps = [s.strip() for s in body.split(";;") if s.strip()]
            macros[name] = steps
            _save_persistent_state()
            console.print(f"[green]Macro saved:[/green] {name} ({len(steps)} steps)")
            return True
        if arg1 == "show" and arg2:
            name = arg2.split(" ", 1)[0]
            if name not in macros:
                console.print("[red]Macro not found.[/red]")
                return True
            for i, s in enumerate(macros[name], 1):
                console.print(f"[cyan]{i}.[/cyan] {rich_escape(s)}")
            return True
        if arg1 == "del" and arg2:
            name = arg2.split(" ", 1)[0]
            if name in macros:
                macros.pop(name)
                _save_persistent_state()
                console.print(f"[green]Macro removed:[/green] {name}")
            else:
                console.print("[red]Macro not found.[/red]")
            return True
        if arg1 == "run" and arg2:
            if macro_depth >= 4:
                console.print("[red]Macro nesting limit reached.[/red]")
                return True
            name = arg2.split(" ", 1)[0]
            if name not in macros:
                console.print("[red]Macro not found.[/red]")
                return True
            steps = macros[name]
            console.print(f"[cyan]Running macro {name} ({len(steps)} steps)...[/cyan]")
            for step in steps:
                step = step.strip()
                if not step:
                    continue
                if step.startswith("/"):
                    if step.lower().startswith("/macro run"):
                        console.print("[yellow]Skipping nested /macro run to avoid loops.[/yellow]")
                        continue
                    handled = _handle_advanced_command(step, client, auth, macro_depth + 1)
                    if not handled:
                        console.print(f"[yellow]Skipped unsupported macro command:[/yellow] {rich_escape(step)}")
                else:
                    _send_prompt(client, step)
            return True
        console.print("[red]Usage: /macro list|add|show|del|run ...[/red]")
        return True
    if cmd == "/bookmark":
        bookmarks = PERSISTENT_STATE["bookmarks"]
        if arg1 in ("list", ""):
            if not bookmarks:
                console.print("[dim]No bookmarks.[/dim]")
            else:
                t = Table(title="Bookmarks", border_style="cyan")
                t.add_column("Name", style="cyan")
                t.add_column("Path", style="white")
                for k, v in sorted(bookmarks.items()):
                    t.add_row(k, rich_escape(v))
                console.print(t)
            return True
        if arg1 == "add" and arg2:
            p2 = arg2.split(" ", 1)
            name = p2[0]
            path = p2[1].strip() if len(p2) > 1 else _cwd
            p = Path(path) if Path(path).is_absolute() else Path(_cwd) / path
            bookmarks[name] = str(p.resolve())
            _save_persistent_state()
            console.print(f"[green]Bookmark saved:[/green] {name}")
            return True
        if arg1 == "go" and arg2:
            name = arg2.split(" ", 1)[0]
            if name not in bookmarks:
                console.print("[red]Bookmark not found.[/red]")
                return True
            target = Path(bookmarks[name])
            if not target.exists() or not target.is_dir():
                console.print("[red]Bookmark path no longer exists.[/red]")
                return True
            _cwd = str(target.resolve())
            console.print(f"[green]cwd -> {rich_escape(_cwd)}[/green]")
            return True
        if arg1 == "del" and arg2:
            name = arg2.split(" ", 1)[0]
            if name in bookmarks:
                bookmarks.pop(name)
                _save_persistent_state()
                console.print(f"[green]Bookmark removed:[/green] {name}")
            else:
                console.print("[red]Bookmark not found.[/red]")
            return True
        console.print("[red]Usage: /bookmark list|add|go|del ...[/red]")
        return True
    if cmd == "/profile":
        profiles = PERSISTENT_STATE["profiles"]
        if arg1 in ("list", ""):
            if not profiles:
                console.print("[dim]No profiles.[/dim]")
            else:
                t = Table(title="Profiles", border_style="cyan")
                t.add_column("Name", style="cyan")
                t.add_column("Model", style="white")
                t.add_column("safe_mode", style="green")
                for n, p in sorted(profiles.items()):
                    t.add_row(n, p.get("model_id", ""), str(p.get("settings", {}).get("safe_mode", True)))
                console.print(t)
            return True
        if arg1 == "save" and arg2:
            name = _sanitize_name(arg2.split(" ", 1)[0])
            profiles[name] = {
                "model_id": client._model_id,
                "settings": copy.deepcopy(APP_SETTINGS),
                "handoff_enabled": HANDOFF_ENABLED,
            }
            _save_persistent_state()
            console.print(f"[green]Profile saved:[/green] {name}")
            return True
        if arg1 == "use" and arg2:
            name = arg2.split(" ", 1)[0]
            if name not in profiles:
                console.print("[red]Profile not found.[/red]")
                return True
            p = profiles[name]
            client._model_id = p.get("model_id", client._model_id)
            APP_SETTINGS.update(p.get("settings", {}))
            PERSISTENT_STATE["settings"] = APP_SETTINGS
            HANDOFF_ENABLED = bool(p.get("handoff_enabled", HANDOFF_ENABLED))
            _save_persistent_state()
            console.print(f"[green]Profile applied:[/green] {name}")
            return True
        if arg1 == "del" and arg2:
            name = arg2.split(" ", 1)[0]
            if name in profiles:
                profiles.pop(name)
                _save_persistent_state()
                console.print(f"[green]Profile removed:[/green] {name}")
            else:
                console.print("[red]Profile not found.[/red]")
            return True
        console.print("[red]Usage: /profile list|save|use|del ...[/red]")
        return True
    if cmd == "/note":
        notes = PERSISTENT_STATE["notes"]
        if arg1 in ("list", ""):
            if not notes:
                console.print("[dim]No notes.[/dim]")
            else:
                for i, n in enumerate(notes, 1):
                    console.print(f"[cyan]{i}.[/cyan] {rich_escape(n)}")
            return True
        if arg1 == "add" and arg2:
            notes.append(arg2)
            if len(notes) > 200:
                del notes[:-200]
            _save_persistent_state()
            console.print("[green]Note added.[/green]")
            return True
        if arg1 == "del" and arg2:
            try:
                idx = int(arg2) - 1
                notes.pop(idx)
                _save_persistent_state()
                console.print("[green]Note removed.[/green]")
            except Exception:
                console.print("[red]Invalid note index.[/red]")
            return True
        if arg1 == "clear":
            notes.clear()
            _save_persistent_state()
            console.print("[green]Notes cleared.[/green]")
            return True
        if arg1 == "push":
            if not notes:
                console.print("[dim]No notes to push.[/dim]")
                return True
            prompt = "Persistent notes:\n" + "\n".join(f"- {n}" for n in notes)
            _send_prompt(client, prompt)
            return True
        console.print("[red]Usage: /note list|add|del|clear|push[/red]")
        return True
    if cmd == "/todo":
        todos = PERSISTENT_STATE["todos"]
        if arg1 in ("list", ""):
            if not todos:
                console.print("[dim]No todos.[/dim]")
            else:
                for i, item in enumerate(todos, 1):
                    if item.get("done"):
                        console.print(f"[green]✓[/green] [dim]{i}. {rich_escape(item.get('text', ''))}[/dim]")
                    else:
                        console.print(f"[yellow]○[/yellow] {i}. {rich_escape(item.get('text', ''))}")
            return True
        if arg1 == "add" and arg2:
            todos.append({"text": arg2, "done": False, "created_at": datetime.now().isoformat(timespec="seconds")})
            _save_persistent_state()
            console.print("[green]Todo added.[/green]")
            return True
        if arg1 in ("done", "undone") and arg2:
            try:
                idx = int(arg2) - 1
                todos[idx]["done"] = (arg1 == "done")
                _save_persistent_state()
                console.print("[green]Todo updated.[/green]")
            except Exception:
                console.print("[red]Invalid todo index.[/red]")
            return True
        if arg1 == "del" and arg2:
            try:
                idx = int(arg2) - 1
                todos.pop(idx)
                _save_persistent_state()
                console.print("[green]Todo removed.[/green]")
            except Exception:
                console.print("[red]Invalid todo index.[/red]")
            return True
        if arg1 == "clear":
            todos.clear()
            _save_persistent_state()
            console.print("[green]Todos cleared.[/green]")
            return True
        console.print("[red]Usage: /todo list|add|done|undone|del|clear[/red]")
        return True
    if cmd == "/pin":
        pins = RUNTIME_STATE["pinned"]
        if arg1 in ("list", ""):
            if not pins:
                console.print("[dim]No pinned items.[/dim]")
            else:
                for i, p in enumerate(pins, 1):
                    console.print(f"[cyan]{i}.[/cyan] {rich_escape(p)}")
            return True
        if arg1 == "last":
            txt = RUNTIME_STATE.get("last_response", "")
            if txt:
                pins.append(txt[:2000])
                console.print("[green]Pinned last response.[/green]")
            else:
                console.print("[dim]No response to pin.[/dim]")
            return True
        if arg1 == "add" and arg2:
            pins.append(arg2)
            console.print("[green]Pinned text.[/green]")
            return True
        if arg1 == "del" and arg2:
            try:
                idx = int(arg2) - 1
                pins.pop(idx)
                console.print("[green]Pin removed.[/green]")
            except Exception:
                console.print("[red]Invalid pin index.[/red]")
            return True
        if arg1 == "clear":
            pins.clear()
            console.print("[green]Pins cleared.[/green]")
            return True
        console.print("[red]Usage: /pin list|last|add|del|clear[/red]")
        return True
    if cmd == "/find" and arg1:
        needle = (arg1 + (" " + arg2 if arg2 else "")).lower()
        hits = []
        for i, item in enumerate(client._history, 1):
            role = item.get("role", "")
            for part in item.get("parts", []):
                text_val = part.get("text", "")
                if text_val and needle in text_val.lower():
                    hits.append((i, role, text_val.replace("\n", " ")[:160]))
        if not hits:
            console.print("[dim]No matches.[/dim]")
        else:
            for i, role, snip in hits[-30:]:
                console.print(f"[cyan]#{i}[/cyan] [{role}] {rich_escape(snip)}")
        return True
    if cmd == "/trim" and arg1:
        try:
            n = max(2, int(arg1))
            before = len(client._history)
            client._history = client._history[-n:]
            console.print(f"[green]History trimmed:[/green] {before} -> {len(client._history)} entries")
        except Exception:
            console.print("[red]Usage: /trim <num_entries>[/red]")
        return True
    if cmd == "/summary":
        user_msgs = sum(1 for h in client._history if h.get("role") == "user")
        model_msgs = sum(1 for h in client._history if h.get("role") == "model")
        console.print(Panel(
            f"[bold]Entries:[/bold] {len(client._history)}\n"
            f"[bold]User msgs:[/bold] {user_msgs}\n"
            f"[bold]Model msgs:[/bold] {model_msgs}\n"
            f"[bold]Last prompt:[/bold] {rich_escape(RUNTIME_STATE.get('last_prompt', '')[:160])}",
            title="Conversation Summary", border_style="cyan"
        ))
        return True
    if cmd == "/diag":
        py = sys.version.split()[0]
        node = subprocess.run(["node", "--version"], capture_output=True, text=True).stdout.strip() or "(missing)"
        gitv = subprocess.run(["git", "--version"], capture_output=True, text=True).stdout.strip() or "(missing)"
        console.print(Panel(
            f"[bold]gcli version:[/bold] {APP_VERSION}\n"
            f"[bold]python:[/bold] {py}\n"
            f"[bold]node:[/bold] {rich_escape(node)}\n"
            f"[bold]git:[/bold] {rich_escape(gitv)}\n"
            f"[bold]cwd exists:[/bold] {Path(_cwd).exists()}\n"
            f"[bold]state file:[/bold] {STATE_FILE}",
            title="Diagnostics", border_style="yellow"
        ))
        return True
    if cmd == "/scan":
        target = arg1 if arg1 else "."
        try:
            p = _resolve_user_path(target)
            ok, err = _enforce_directory_access(p, f"scan workspace at {p}", for_write=False)
            if not ok:
                console.print(f"[red]{rich_escape(err)}[/red]")
                return True
            data = _scan_workspace(target)
            t = Table(title=f"Workspace Scan: {data['path']}", border_style="cyan")
            t.add_column("Metric", style="cyan")
            t.add_column("Value", style="white")
            t.add_row("files", str(data["files"]))
            t.add_row("directories", str(data["dirs"]))
            t.add_row("total_size_bytes", str(data["total_size"]))
            console.print(t)
            t2 = Table(title="Top Extensions", border_style="magenta")
            t2.add_column("Extension", style="magenta")
            t2.add_column("Count", justify="right", style="green")
            for ext, cnt in data["extensions"]:
                t2.add_row(ext, str(cnt))
            console.print(t2)
        except Exception as e:
            console.print(f"[red]{rich_escape(str(e))}[/red]")
        return True
    if cmd == "/session":
        if arg1 in ("list", ""):
            SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
            files = sorted(SESSIONS_DIR.glob("*.json"))
            if not files:
                console.print("[dim]No sessions saved.[/dim]")
            else:
                for f in files:
                    console.print(f"[cyan]{f.stem}[/cyan]  [dim]{datetime.fromtimestamp(f.stat().st_mtime)}[/dim]")
            return True
        if arg1 == "save":
            name = _sanitize_name(arg2 if arg2 else "manual")
            p = _session_path(name)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(_collect_session_payload(client), indent=2, ensure_ascii=False), encoding="utf-8")
            console.print(f"[green]Session saved:[/green] {rich_escape(str(p))}")
            return True
        if arg1 == "load" and arg2:
            name = _sanitize_name(arg2)
            p = _session_path(name)
            if not p.exists():
                console.print("[red]Session not found.[/red]")
                return True
            payload = json.loads(p.read_text(encoding="utf-8"))
            _apply_session_payload(client, payload)
            console.print(f"[green]Session loaded:[/green] {name}")
            return True
        if arg1 == "export" and arg2:
            p = Path(arg2) if Path(arg2).is_absolute() else Path(_cwd) / arg2
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(_collect_session_payload(client), indent=2, ensure_ascii=False), encoding="utf-8")
            console.print(f"[green]Session exported:[/green] {rich_escape(str(p.resolve()))}")
            return True
        if arg1 == "import" and arg2:
            p = Path(arg2) if Path(arg2).is_absolute() else Path(_cwd) / arg2
            if not p.exists():
                console.print("[red]Import file not found.[/red]")
                return True
            payload = json.loads(p.read_text(encoding="utf-8"))
            _apply_session_payload(client, payload)
            console.print("[green]Session imported.[/green]")
            return True
        if arg1 == "new":
            _capture_undo_snapshot(client, "session-new")
            client._history.clear()
            RUNTIME_STATE["session_id"] = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
            RUNTIME_STATE["last_prompt"] = ""
            RUNTIME_STATE["last_response"] = ""
            console.print("[green]Started a new in-memory session.[/green]")
            return True
        console.print("[red]Usage: /session list|save|load|export|import|new[/red]")
        return True
    if cmd == "/transcript":
        target = arg1 if arg1 else ""
        if arg2:
            target = f"{arg1} {arg2}".strip()
        if not target:
            TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
            target = str((TRANSCRIPTS_DIR / f"transcript-{datetime.now().strftime('%Y%m%d-%H%M%S')}.md").resolve())
        p = _save_transcript(client, target)
        console.print(f"[green]Transcript saved:[/green] {rich_escape(str(p))}")
        return True
    if cmd == "/undo":
        if not RUNTIME_STATE["undo_stack"]:
            console.print("[dim]Nothing to undo.[/dim]")
            return True
        snap = RUNTIME_STATE["undo_stack"].pop()
        RUNTIME_STATE["redo_stack"].append({
            "label": "redo",
            "history": copy.deepcopy(client._history),
            "model_id": client._model_id,
            "cwd": _cwd,
        })
        client._history = snap["history"]
        client._model_id = snap["model_id"]
        _cwd = snap["cwd"]
        console.print(f"[green]Undid:[/green] {rich_escape(snap['label'])}")
        return True
    if cmd == "/redo":
        if not RUNTIME_STATE["redo_stack"]:
            console.print("[dim]Nothing to redo.[/dim]")
            return True
        snap = RUNTIME_STATE["redo_stack"].pop()
        RUNTIME_STATE["undo_stack"].append({
            "label": "undo",
            "history": copy.deepcopy(client._history),
            "model_id": client._model_id,
            "cwd": _cwd,
        })
        client._history = snap["history"]
        client._model_id = snap["model_id"]
        _cwd = snap["cwd"]
        console.print("[green]Redo applied.[/green]")
        return True
    if cmd == "/tag":
        tags = RUNTIME_STATE["session_tags"]
        if arg1 in ("list", ""):
            console.print(", ".join(tags) if tags else "[dim]No tags.[/dim]")
            return True
        if arg1 == "add" and arg2:
            tags.append(arg2)
            console.print("[green]Tag added.[/green]")
            return True
        if arg1 == "del" and arg2:
            try:
                tags.remove(arg2)
                console.print("[green]Tag removed.[/green]")
            except ValueError:
                console.print("[red]Tag not found.[/red]")
            return True
        if arg1 == "clear":
            tags.clear()
            console.print("[green]Tags cleared.[/green]")
            return True
        console.print("[red]Usage: /tag list|add|del|clear[/red]")
        return True
    if cmd == "/compact":
        n = len(client._history)
        if n < 4:
            console.print("[dim]Conversation is already short — nothing to compact.[/dim]")
            return True
        _capture_undo_snapshot(client, "pre-compact")
        console.print(f"[dim]Compacting {n} history entries...[/dim]")
        _send_prompt(client,
            "Please provide a thorough but concise summary of our entire conversation so far. "
            "Include: all tasks requested, what was built or changed, important decisions, "
            "code written, errors encountered and resolved, and current state of any project. "
            "Be comprehensive — this summary will replace the full conversation history for context."
        )
        summary = RUNTIME_STATE.get("last_response", "")
        if not summary:
            console.print("[yellow]Could not get summary. History unchanged.[/yellow]")
            return True
        client._history = [
            {"role": "user", "parts": [{"text": f"[Conversation summary — {n} messages compacted]\n\n{summary}"}]},
            {"role": "model", "parts": [{"text": "Got it. I have full context from the summary and am ready to continue."}]},
        ]
        console.print(f"[green]✓ Compacted {n} entries → 2  (use /undo to restore)[/green]")
        return True
    return False


def _make_prompt(model_id: str) -> str:
    """Build a context-aware input prompt showing abbreviated cwd and model."""
    p = Path(_cwd)
    home = Path.home()
    try:
        rel = p.relative_to(home)
        cwd_str = "~/" + str(rel).replace("\\", "/") if str(rel) != "." else "~"
    except ValueError:
        parts = p.parts
        cwd_str = "/".join(parts[-2:]).replace("\\", "/") if len(parts) >= 2 else str(p)
    if len(cwd_str) > 45:
        cwd_str = "…" + cwd_str[-42:]
    # Shorten long model names for the prompt
    model_short = model_id
    for pfx in ("gemini-", "claude-", "gpt-"):
        model_short = model_short.replace(pfx, "")
    model_short = model_short.replace("-exp-01-21", "").replace("-20251001", "")
    if len(model_short) > 20:
        model_short = model_short[:18] + "…"
    return f"[dim]{cwd_str}[/dim] [dim]({model_short})[/dim] [bold cyan]›[/bold cyan]"


def _read_user_input(model_id: str) -> str:
    """Read user input with optional multi-line continuation (end line with \\)."""
    prompt_str = _make_prompt(model_id)
    try:
        line = Prompt.ask(f"\n{prompt_str}")
    except EOFError:
        return ""
    if not line.endswith("\\"):
        return line
    lines = [line[:-1]]
    while True:
        try:
            cont = Prompt.ask("  [dim]...[/dim]")
        except EOFError:
            break
        if cont.endswith("\\"):
            lines.append(cont[:-1])
        else:
            lines.append(cont)
            break
    return "\n".join(lines)


def print_banner():
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    art = Text(_banner_text().strip("\n"), style="brand")
    meta = Text.from_markup(
        f"[muted]cwd[/muted]: [accent]{rich_escape(_cwd)}[/accent]   [muted]time[/muted]: {now}"
    )
    body = Text()
    body.append_text(art)
    body.append("\n")
    body.append_text(Text.from_markup("[muted]Autonomous terminal AI for coding workflows[/muted]"))
    body.append("\n")
    body.append_text(meta)
    console.print(
        Panel(
            body,
            title=f"[brand]GCLI v{APP_VERSION}[/brand]",
            border_style="brand",
            padding=(1, 2),
            subtitle="[muted]/help for commands[/muted]",
        )
    )


def _print_ready_panel(model_id: str, cleared: bool = False) -> None:
    state_line = "[muted]conversation cleared[/muted]" if cleared else "[muted]new session[/muted]"
    console.print(Panel(
        f"[brand]Ready[/brand]  [muted]|[/muted]  model [accent]{rich_escape(model_id)}[/accent]  [muted]|[/muted]  {state_line}\n"
        "[muted]Ask for coding tasks, file edits, and terminal operations[/muted]\n"
        "[muted]/help for commands · exit to quit[/muted]",
        border_style="accent", padding=(0, 1)
    ))



def main():
    _bootstrap_output_encoding()
    global SHOW_BANNER
    args = parse_args()
    if args.version:
        print(f"GCLI v{APP_VERSION}")
        return
    SHOW_BANNER = not args.no_banner
    if not args.prompt and not sys.stdin.isatty():
        console.print("[err]Interactive mode requires a TTY. Use --prompt for one-shot non-interactive usage.[/err]")
        sys.exit(2)
    if SHOW_BANNER:
        print_banner()
    allow_prompt = (not bool(args.prompt)) and sys.stdin.isatty()
    try:
        auth = get_auth(allow_prompt=allow_prompt)
    except Exception as e:
        console.print(f"[err]{rich_escape(str(e))}[/err]")
        sys.exit(1)
    if args.model:
        model_id = args.model.strip()
    elif APP_SETTINGS.get("default_model"):
        model_id = APP_SETTINGS["default_model"].strip()
        console.print(f"[green]OK[/green] Using default_model from settings: [cyan]{model_id}[/cyan]\n")
    else:
        model_id = select_model(auth["mode"], interactive=allow_prompt)

    try:
        client = GcliClient(auth=auth, model_id=model_id)
    except Exception as e:
        console.print(f"[red]Failed to initialize: {e}[/red]")
        sys.exit(1)

    _setup_readline()

    if args.prompt:
        _send_prompt(client, args.prompt)
        _save_readline_history()
        return

    _print_ready_panel(model_id)

    while True:
        try:
            user_input = _read_user_input(client._model_id)
            if not user_input.strip():
                continue

            user_input = _expand_alias(user_input)
            low = user_input.strip().lower()

            if low in ["exit", "quit", "bye", ":q"]:
                _save_readline_history()
                uptime = _format_uptime(time.time() - RUNTIME_STATE["started_at"])
                msg = f"[dim]session ended · uptime {uptime}[/dim]"
                with Live("", refresh_per_second=20, transient=False) as _live:
                    for i in range(len(msg)):
                        _live.update(Text.from_markup(msg[: i + 1]))
                        time.sleep(0.012)
                console.print()
                break

            if low == "/cwd":
                console.print(f"[cyan]cwd: {rich_escape(_cwd)}[/cyan]")
                continue

            if low == "/history":
                _print_tool_history(client)
                continue

            if low == "/clear":
                client._history.clear()
                console.clear()
                if SHOW_BANNER:
                    print_banner()
                _print_ready_panel(client._model_id, cleared=True)
                continue

            if low == "/handoff":
                global HANDOFF_ENABLED
                HANDOFF_ENABLED = not HANDOFF_ENABLED
                state = "[green]ENABLED[/green]" if HANDOFF_ENABLED else "[red]DISABLED[/red]"
                console.print(f"\n🤖 [bold cyan]Agent Handoff is now {state}[/bold cyan]")
                console.print("[dim]The AI can now use the `delegate_task` tool to spawn sub-agents for parallel or isolated tasks.\n[/dim]")
                continue
                
            if user_input.strip().lower().startswith("/model"):
                parts_cmd = user_input.strip().split(" ", 1)
                if len(parts_cmd) > 1 and parts_cmd[1].strip():
                    new_model = parts_cmd[1].strip()
                else:
                    new_model = select_model(auth["mode"])
                model_id = new_model
                client.switch_model(new_model)
                prov = _detect_provider(new_model)
                console.print(f"[bright_green]✓[/bright_green] Now using [bold cyan]{model_id}[/bold cyan] [dim]({prov})[/dim]")
                continue

            if low == "/forget-key":
                if SAVED_KEY_FILE.exists():
                    SAVED_KEY_FILE.unlink()
                    console.print("[green]✓ Saved key deleted. Next run will prompt for a new key.[/green]")
                else:
                    console.print("[dim]No saved key found.[/dim]")
                continue

            if low.startswith("/switch-key"):
                # /switch-key [provider] — switch key for current or specified provider
                parts_sk = low.split(None, 1)
                target_prov = parts_sk[1].strip() if len(parts_sk) > 1 else client._provider
                valid_providers = ["gemini", "openai", "anthropic"]
                if target_prov not in valid_providers:
                    console.print(f"[yellow]Unknown provider '{target_prov}'. Use: {', '.join(valid_providers)}[/yellow]")
                    continue
                if target_prov == "gemini":
                    console.print("[dim]Enter your Gemini API key (from aistudio.google.com):[/dim]")
                    new_key = _manual_key_flow()
                    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
                    SAVED_KEY_FILE.write_text(new_key, encoding="utf-8")
                    _save_provider_key("gemini", new_key)
                    if client._provider == "gemini":
                        client._sdk = genai.Client(api_key=new_key)
                        client._auth = {"mode": "apikey", "api_key": new_key}
                        auth = client._auth
                else:
                    console.print(f"[dim]Enter your {target_prov.title()} API key:[/dim]")
                    new_key = Prompt.ask(f"[bold]{target_prov.title()} API key[/bold] (hidden)", password=True).strip()
                    if new_key:
                        _save_provider_key(target_prov, new_key)
                        if client._provider == target_prov:
                            client._provider_key = new_key
                            client._init_provider(allow_prompt=False)
                console.print(f"[green]✓ {target_prov.title()} API key updated.[/green]")
                continue

            if low == "/help":
                console.print()

                t_core = Table(title="[bold bright_cyan]Core[/bold bright_cyan]", border_style="cyan",
                               show_header=False, padding=(0, 1))
                t_core.add_column("Cmd", style="bold cyan", no_wrap=True)
                t_core.add_column("Description", style="white")
                for cmd_name, desc in [
                    ("/model [id]",     "Switch model mid-session (no arg = show picker)"),
                    ("/clear",          "Clear conversation history and screen"),
                    ("/history [N]",    "Show tool execution log (last N events)"),
                    ("/status",         "Session overview: model, cwd, stats, settings"),
                    ("/stats",          "Detailed runtime stats and top tools"),
                    ("/last",           "Re-print the last AI response"),
                    ("/retry",          "Re-send the last prompt"),
                    ("/cwd  /pwd",      "Show current working directory"),
                    ("/cd <path>",      "Change working directory"),
                    ("/ls [pattern]",   "List files in cwd (optional glob filter)"),
                    ("/tree [path]",    "Tree view of a directory"),
                    ("/handoff",        "Toggle AI-to-AI sub-agent delegation"),
                    ("/switch-key [p]",  "Update API key (p = gemini/openai/anthropic)"),
                    ("/forget-key",     "Delete the saved Gemini API key"),
                    ("exit / quit",     "End the session"),
                ]:
                    t_core.add_row(cmd_name, desc)
                console.print(t_core)

                console.print()
                t_session = Table(title="[bold bright_cyan]Session & History[/bold bright_cyan]", border_style="cyan",
                                  show_header=False, padding=(0, 1))
                t_session.add_column("Cmd", style="bold cyan", no_wrap=True)
                t_session.add_column("Description", style="white")
                for cmd_name, desc in [
                    ("/session list",          "List saved sessions"),
                    ("/session save [name]",   "Save current session"),
                    ("/session load <name>",   "Restore a saved session"),
                    ("/session new",           "Start a fresh in-memory session"),
                    ("/session export <path>", "Export session to a file"),
                    ("/session import <path>", "Import session from a file"),
                    ("/transcript [path]",     "Export conversation as Markdown"),
                    ("/undo  /redo",           "Undo/redo conversation snapshots"),
                    ("/compact",               "Compress history to a summary (saves tokens)"),
                    ("/find <text>",           "Search conversation history"),
                    ("/trim <n>",              "Keep only the last N history entries"),
                    ("/summary",               "Conversation stats summary"),
                    ("/replay [index]",        "Re-run a previous shell command"),
                ]:
                    t_session.add_row(cmd_name, desc)
                console.print(t_session)

                console.print()
                t_data = Table(title="[bold bright_cyan]Data & Productivity[/bold bright_cyan]", border_style="cyan",
                               show_header=False, padding=(0, 1))
                t_data.add_column("Cmd", style="bold cyan", no_wrap=True)
                t_data.add_column("Description", style="white")
                for cmd_name, desc in [
                    ("/todo list|add|done|undone|del|clear",     "Persistent todo list"),
                    ("/note list|add|del|clear|push",            "Persistent notes (push sends to AI)"),
                    ("/pin list|last|add|del|clear",             "Pin AI responses for later"),
                    ("/bookmark list|add|go|del",                "Bookmark directory paths"),
                    ("/snippet list|add|show|run|del",           "Reusable prompt templates with variables"),
                    ("/macro list|add|show|run|del",             "Multi-step command sequences"),
                    ("/alias list|add|del",                      "Command shortcuts"),
                    ("/profile list|save|use|del",               "Save/restore settings + model profiles"),
                    ("/tag list|add|del|clear",                  "Tag the current session"),
                ]:
                    t_data.add_row(cmd_name, desc)
                console.print(t_data)

                console.print()
                t_cfg = Table(title="[bold bright_cyan]Settings & Permissions[/bold bright_cyan]", border_style="cyan",
                              show_header=False, padding=(0, 1))
                t_cfg.add_column("Cmd", style="bold cyan", no_wrap=True)
                t_cfg.add_column("Description", style="white")
                for cmd_name, desc in [
                    ("/settings",                       "Show all current settings"),
                    ("/set <key> <value>",              "Change a setting (e.g. /set safe_mode false)"),
                    ("/get <key>",                      "Read a single setting value"),
                    ("/reset-setting <key>",            "Reset a setting to its default"),
                    ("/perm list",                      "Show directory permission state"),
                    ("/perm mode <prompt|allow-all>",   "Change permission mode"),
                    ("/perm trust <path>",              "Permanently trust a directory"),
                    ("/perm untrust <path>",            "Remove a trusted directory"),
                    ("/perm once <path>",               "Allow access once"),
                    ("/perm check <path>",              "Check if a path is allowed"),
                ]:
                    t_cfg.add_row(cmd_name, desc)
                console.print(t_cfg)

                console.print()
                t_diag = Table(title="[bold bright_cyan]Diagnostics[/bold bright_cyan]", border_style="cyan",
                               show_header=False, padding=(0, 1))
                t_diag.add_column("Cmd", style="bold cyan", no_wrap=True)
                t_diag.add_column("Description", style="white")
                for cmd_name, desc in [
                    ("/diag",           "Environment diagnostics (Python, Node, Git)"),
                    ("/scan [path]",    "Workspace file stats and extension breakdown"),
                    ("/version",        "Show GCLI version"),
                    ("/uptime",         "Session uptime"),
                    ("/time  /date",    "Current time / date"),
                    ("/session-id",     "Show current session ID"),
                    ("/clear-tools",    "Reset tool call counters"),
                    ("/echo <text>",    "Print text (useful in macros)"),
                ]:
                    t_diag.add_row(cmd_name, desc)
                console.print(t_diag)

                console.print()
                examples = [
                    "what files are in this folder?",
                    "create a Flask REST API with 3 endpoints",
                    "fix the bug in main.py",
                    "search the web for the latest numpy release",
                    "write a 3D snake game and run it",
                    "git status and summarize changes",
                ]
                console.print(Panel(
                    "\n".join(f"  [dim]>[/dim] {e}" for e in examples) +
                    "\n\n  [dim]Tip: end a line with \\ to continue on the next line[/dim]",
                    title="[bold bright_cyan]Example Prompts[/bold bright_cyan]",
                    border_style="cyan", padding=(0, 1)
                ))
                continue

            if _handle_advanced_command(user_input, client, auth):
                continue

            _send_prompt(client, user_input)

        except KeyboardInterrupt:
            console.print("\n[dim]Interrupted. Ctrl+C again to exit.[/dim]")
            try:
                time.sleep(0.5)
            except KeyboardInterrupt:
                _save_readline_history()
                console.print("\n[dim]Session ended.[/dim]")
                break

        except Exception as e:
            err = str(e)
            err_low = err.lower()
            prov = client._provider if client else "gemini"

            # Auth errors
            if any(kw in err for kw in ("API_KEY_INVALID", "API key not valid", "invalid_api_key",
                                         "authentication_error", "AuthenticationError")):
                console.print(Panel(
                    f"[bold red]❌ Invalid {prov.title()} API Key[/bold red]\n\n"
                    "Your API key was rejected.\n\n"
                    f"Run [bold]/switch-key {prov}[/bold] to enter a valid key.",
                    title="[red]Auth Error[/red]", border_style="red"
                ))
            # Model not found
            elif ("NOT_FOUND" in err and "not found for API version" in err) or "model_not_found" in err_low or "does not exist" in err_low:
                console.print(Panel(
                    f"[bold yellow]⚠️  Model not available: [cyan]{rich_escape(model_id)}[/cyan][/bold yellow]\n\n"
                    "That model ID is not valid for this provider/API version.\n\n"
                    "Type [bold cyan]/model[/bold cyan] to pick a different model.",
                    title="[yellow]Model Not Found[/yellow]", border_style="yellow"
                ))
            # Hard quota exhausted (not retried) — daily limit
            elif "quota" in err_low or "RESOURCE_EXHAUSTED" in err or "rate_limit" in err_low or "429" in err:
                tip = ""
                if prov == "gemini":
                    tip = ("\n\n[bold]Options:[/bold]\n"
                           "  1. Wait a minute and try again\n"
                           "  2. [bold cyan]/switch-key gemini[/bold cyan] to use your own free API key\n"
                           "  3. [bold cyan]/model[/bold cyan] to pick a higher-quota model (e.g. gemini-2.5-flash-lite)")
                elif prov == "openai":
                    tip = "\n\nCheck your OpenAI usage limits at platform.openai.com/usage"
                elif prov == "anthropic":
                    tip = "\n\nCheck your Anthropic usage limits at console.anthropic.com"
                console.print(Panel(
                    f"[bold yellow]⚠️  Rate limited / Quota exhausted ({prov.title()})[/bold yellow]{tip}",
                    title="[yellow]Rate Limited[/yellow]", border_style="yellow"
                ))
            else:
                console.print(f"\n[bold red]Error:[/bold red] {rich_escape(err)}")
                console.print(f"[dim]{rich_escape(traceback.format_exc())}[/dim]")
            console.print("[dim]You can continue or type 'exit'.[/dim]")


if __name__ == "__main__":
    main()
