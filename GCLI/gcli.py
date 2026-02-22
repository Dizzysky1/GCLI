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
from pathlib import Path
from datetime import datetime

# ─── Dependency check ────────────────────────────────────────────────────────
try:
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.markup import escape as rich_escape
    from rich.prompt import Prompt
    from rich.panel import Panel
    from rich.table import Table
    from rich.syntax import Syntax
    from google import genai
    from google.genai import types as gt
except ImportError as e:
    print(f"Missing dependency: {e}\nRun: pip install -r requirements.txt")
    sys.exit(1)

# ─── Constants ───────────────────────────────────────────────────────────────
console = Console()
_cwd = os.getcwd()
CONFIG_DIR = Path.home() / ".gcli"
SAVED_KEY_FILE = CONFIG_DIR / "apikey.txt"
AISTUDIO_URL = "https://aistudio.google.com/app/apikey"

# Gemini CLI integration paths
GEMINI_CLI_DIR = Path.home() / ".gemini"
GEMINI_CLI_ACCOUNTS = GEMINI_CLI_DIR / "google_accounts.json"
# Path to the helper script that reads the Gemini CLI's stored API key
_GEMINI_KEY_SCRIPT = Path(__file__).parent / "get_gemini_key.mjs"

BANNER = r"""
   ██████╗  ██████╗██╗     ██╗
  ██╔════╝ ██╔════╝██║     ██║
  ██║  ███╗██║     ██║     ██║
  ██║   ██║██║     ██║     ██║
  ╚██████╔╝╚██████╗███████╗██║
   ╚═════╝  ╚═════╝╚══════╝╚═╝
"""

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

## Operating Principles
1. **Act, don't ask**: Full permission to use any tool. Never ask for confirmation.
2. **Be autonomous**: Complete tasks end-to-end. Explore → act → verify.
3. **Iterate**: run command → read output → run next command. Chain tools freely.
4. **Verify**: After completing a task, confirm it actually works.
5. **PowerShell syntax**: You're on Windows.
6. **Be concise**: One-line plan, use tools, brief summary of result.
7. **edit_file** for small changes, **write_file** when rewriting whole files.
8. **Think ahead**: Briefly stream your thought process before using tools. For complex tasks, write out a brief To-Do list of steps to accomplish the goal, and explicitly note backup plans ahead of time just in case your primary method fails.
9. **Find lost files**: If the user asks about a project or file but you don't know the exact path, use aggressive broad search commands (e.g. `cmd.exe /c "dir C:\*gcli* /s /b /ad"` or `Get-ChildItem -ErrorAction SilentlyContinue`) instead of blindly guessing paths.
10. **Error Recovery**: If a tool fails (e.g., run_command returns an error), DO NOT just give up. Read the error, explain what went wrong, and immediately execute your backup plan or try an alternative approach (e.g. adjust the path, search the web, install the missing package).

OS: Windows | Shell: PowerShell | Working dir changes persist per session."""

# ══════════════════════════════════════════════════════════════════════════════
#  TOOL IMPLEMENTATIONS
# ══════════════════════════════════════════════════════════════════════════════

def _tool_run_command(command: str, timeout: int = 120) -> dict:
    console.print(Panel(f"[bold yellow]{rich_escape(command)}[/bold yellow]",
                        title="[bold yellow]⚡ run_command[/bold yellow]",
                        border_style="yellow", padding=(0, 1)))
    try:
        start = time.time()
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, timeout=timeout,
            cwd=_cwd, encoding="utf-8", errors="replace"
        )
        elapsed = time.time() - start
        out = (result.stdout.strip() + ("\n" + result.stderr.strip() if result.stderr.strip() else "")).strip() or "(no output)"
        if len(out) > 4000:
            out = out[:2000] + "\n...[TRUNCATED]\n" + out[-2000:]
        ok = result.returncode == 0
        console.print(Panel(rich_escape(out),
                            title=f"[{'green' if ok else 'red'}]{'✅' if ok else '❌'} Exit {result.returncode} ({elapsed:.1f}s)[/{'green' if ok else 'red'}]",
                            border_style="green" if ok else "red", padding=(0, 1)))
        return {"stdout": result.stdout.strip(), "stderr": result.stderr.strip(),
                "exit_code": result.returncode, "success": ok}
    except subprocess.TimeoutExpired:
        msg = f"Timed out after {timeout}s"
        console.print(Panel(msg, title="[red]Timeout[/red]", border_style="red"))
        return {"stdout": "", "stderr": msg, "exit_code": -1, "success": False}
    except Exception as e:
        console.print(Panel(str(e), title="[red]Error[/red]", border_style="red"))
        return {"stdout": "", "stderr": str(e), "exit_code": -1, "success": False}


def _tool_read_file(path: str, start_line: int = 1, end_line: int = 0) -> dict:
    console.print(f"[dim]📄 read_file: [cyan]{rich_escape(path)}[/cyan][/dim]")
    try:
        p = Path(path) if Path(path).is_absolute() else Path(_cwd) / path
        if not p.exists(): return {"success": False, "error": f"Not found: {path}"}
        if not p.is_file(): return {"success": False, "error": f"Not a file: {path}"}
        if p.stat().st_size > 5_000_000:
            return {"success": False, "error": "File too large (>5 MB). Use run_command."}
        content = p.read_text(encoding="utf-8", errors="replace")
        lines = content.splitlines()
        total = len(lines)
        snippet = "\n".join(lines[max(0, start_line - 1):(end_line or total)])
        lang = {"py":"python","js":"javascript","ts":"typescript","html":"html","css":"css",
                "json":"json","md":"markdown","sh":"bash","ps1":"powershell",
                "yml":"yaml","yaml":"yaml"}.get(p.suffix.lstrip("."), "text")
        preview = snippet[:3000] + ("\n...[truncated]..." if len(snippet) > 3000 else "")
        console.print(Syntax(preview, lang, theme="monokai", line_numbers=True,
                              start_line=start_line, word_wrap=False))
        return {"success": True, "content": snippet, "total_lines": total, "path": str(p.resolve())}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _tool_write_file(path: str, content: str, overwrite: bool = True) -> dict:
    console.print(f"[dim]✍️  write_file: [cyan]{rich_escape(path)}[/cyan][/dim]")
    try:
        p = Path(path) if Path(path).is_absolute() else Path(_cwd) / path
        if p.exists() and not overwrite:
            return {"success": False, "error": "File exists. Set overwrite=true."}
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        console.print(f"[green]✅ Written {p.stat().st_size} bytes → {rich_escape(str(p.resolve()))}[/green]")
        return {"success": True, "path": str(p.resolve()), "size_bytes": p.stat().st_size}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _tool_edit_file(path: str, old_str: str, new_str: str) -> dict:
    console.print(f"[dim]✏️  edit_file: [cyan]{rich_escape(path)}[/cyan][/dim]")
    try:
        p = Path(path) if Path(path).is_absolute() else Path(_cwd) / path
        if not p.exists(): return {"success": False, "error": f"Not found: {path}"}
        original = p.read_text(encoding="utf-8", errors="replace")
        if old_str not in original:
            return {"success": False, "error": "old_str not found in file — no replacement made."}
        p.write_text(original.replace(old_str, new_str, 1), encoding="utf-8")
        console.print("[green]✅ Edited successfully.[/green]")
        return {"success": True, "path": str(p.resolve())}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _tool_list_directory(path: str = ".", pattern: str = "*") -> dict:
    target = path if path != "." else _cwd
    console.print(f"[dim]📁 list_directory: [cyan]{rich_escape(target)}[/cyan][/dim]")
    try:
        p = Path(target) if Path(target).is_absolute() else Path(_cwd) / target
        if not p.exists(): return {"success": False, "error": f"Not found: {path}"}
        entries = []
        for item in sorted(p.iterdir()):
            if not fnmatch.fnmatch(item.name, pattern): continue
            entries.append({"name": item.name,
                            "type": "directory" if item.is_dir() else "file",
                            **( {"size_bytes": item.stat().st_size} if item.is_file() else {} )})
        table = Table(title=f"📁 {rich_escape(str(p.resolve()))}", border_style="cyan")
        table.add_column("Name"); table.add_column("Type", style="dim"); table.add_column("Size", justify="right", style="green")
        for e in entries[:100]:
            icon = "📁" if e["type"] == "directory" else "📄"
            table.add_row(f"{icon} {e['name']}", e["type"], str(e.get("size_bytes", "")))
        console.print(table)
        return {"success": True, "path": str(p.resolve()), "entries": entries}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _tool_change_directory(path: str) -> dict:
    global _cwd
    console.print(f"[dim]📂 change_directory: [cyan]{rich_escape(path)}[/cyan][/dim]")
    try:
        p = (Path(path) if Path(path).is_absolute() else Path(_cwd) / path).resolve()
        if not p.exists(): return {"success": False, "error": f"Not found: {path}"}
        if not p.is_dir(): return {"success": False, "error": f"Not a directory: {path}"}
        _cwd = str(p)
        console.print(f"[green]✅ cwd → {rich_escape(_cwd)}[/green]")
        return {"success": True, "cwd": _cwd}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _tool_search_files(path: str, pattern: str, content_search: str = "") -> dict:
    target = str(Path(path).resolve() if Path(path).is_absolute() else (Path(_cwd) / path).resolve())
    console.print(f"[dim]🔍 search_files: [cyan]{rich_escape(target)}[/cyan] | [yellow]{rich_escape(pattern)}[/yellow][/dim]")
    try:
        matches = []
        for m in Path(target).rglob(pattern):
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
        for m in matches[:20]: console.print(f"  [cyan]{rich_escape(m)}[/cyan]")
        if len(matches) > 20: console.print(f"  [dim]+{len(matches)-20} more[/dim]")
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
    # Give the sub-agent a modified system prompt emphasizing it's a sub-agent
    global SYSTEM_PROMPT
    original_prompt = SYSTEM_PROMPT
    client._history.append({
        "role": "user",
        "parts": [{"text": f"You are a sub-agent executing a delegated task. Focus entirely on completing this task, and return a summary of results. The task is:\n\n{task}"}]
    })
    
    result_text = ""
    # We will block and use the standard stream, but NOT update the main live UI directly, 
    # since we want to print its progress independently.
    
    from rich.live import Live
    live = Live(auto_refresh=True, refresh_per_second=10)
    live.start()
    
    try:
        MAX_ROUNDS = 10
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


import shutil

def _tool_delete_file(path: str) -> dict:
    console.print(f"[dim]🗑️  delete_file: [cyan]{rich_escape(path)}[/cyan][/dim]")
    try:
        p = Path(path) if Path(path).is_absolute() else Path(_cwd) / path
        if not p.exists(): return {"success": False, "error": f"Not found: {path}"}
        if p.is_dir():
            shutil.rmtree(p)
            console.print(f"[green]✅ Deleted directory: {rich_escape(str(p.resolve()))}[/green]")
        else:
            p.unlink()
            console.print(f"[green]✅ Deleted file: {rich_escape(str(p.resolve()))}[/green]")
        return {"success": True, "path": str(p.resolve())}
    except Exception as e:
        return {"success": False, "error": str(e)}

def _tool_create_directory(path: str) -> dict:
    console.print(f"[dim]📁 create_directory: [cyan]{rich_escape(path)}[/cyan][/dim]")
    try:
        p = Path(path) if Path(path).is_absolute() else Path(_cwd) / path
        p.mkdir(parents=True, exist_ok=True)
        console.print(f"[green]✅ Created directory: {rich_escape(str(p.resolve()))}[/green]")
        return {"success": True, "path": str(p.resolve())}
    except Exception as e:
        return {"success": False, "error": str(e)}

def _tool_move_file(source: str, destination: str) -> dict:
    console.print(f"[dim]🚚 move_file: [cyan]{rich_escape(source)}[/cyan] → [cyan]{rich_escape(destination)}[/cyan][/dim]")
    try:
        src = Path(source) if Path(source).is_absolute() else Path(_cwd) / source
        dst = Path(destination) if Path(destination).is_absolute() else Path(_cwd) / destination
        if not src.exists(): return {"success": False, "error": f"Source not found: {source}"}
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        console.print(f"[green]✅ Moved to: {rich_escape(str(dst.resolve()))}[/green]")
        return {"success": True, "source": str(src.resolve()), "destination": str(dst.resolve())}
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
}


# ══════════════════════════════════════════════════════════════════════════════
#  GEMINI TOOL SCHEMA
# ══════════════════════════════════════════════════════════════════════════════

HANDOFF_ENABLED = False


def _build_bridge_tools() -> list:
    """Build the raw generic JSON dictionaries for the Node.js bridge."""
    specs = [
        ("run_command", "Execute a PowerShell command on the user's Windows machine. Use for installing packages, running scripts, git, compiling, system info, etc. Can be multi-line.", [
            ("command", "string", "PowerShell command or script to execute.", True),
            ("timeout", "integer", "Timeout in seconds. Default 120.", False),
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
        ("search_files", "Recursively search for files by glob pattern.", [
            ("path", "string", "Root search directory.", True),
            ("pattern", "string", "Glob pattern e.g. '*.py'.", True),
            ("content_search", "string", "Only return files containing this string.", False),
        ]),
    ]
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
    specs = [
        ("run_command", "Execute a PowerShell command on the user's Windows machine. Use for installing packages, running scripts, git, compiling, system info, etc. Can be multi-line.", [
            ("command", "string", "PowerShell command or script to execute.", True),
            ("timeout", "integer", "Timeout in seconds. Default 120.", False),
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
        ("edit_file", "Surgically replace the FIRST occurrence of old_str with new_str. old_str must match EXACTLY (including whitespace). Safer than write_file for small changes.", [
            ("path", "string", "File path.", True),
            ("old_str", "string", "Exact string to find.", True),
            ("new_str", "string", "Replacement string.", True),
        ]),
        ("list_directory", "List files and directories at a path.", [
            ("path", "string", "Directory to list.", True),
            ("pattern", "string", "Optional glob filter e.g. '*.py'.", False),
        ]),
        ("change_directory", "Change working directory for subsequent commands and relative paths.", [
            ("path", "string", "Directory to switch into.", True),
        ]),
                ("delegate_task", "Delegate a sub-task to be solved completely by another AI model. Useful for delegating complex, isolated analysis or creating scripts while you do something else. The sub-agent has the SAME tool access as you. Only available if handoff is enabled.", [
            ("task", "string", "A crystal clear description of exactly what the sub-agent should do.", True),
            ("model", "string", "The Gemini model ID to use (e.g. gemini-2.5-flash)", False),
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
        ("search_files", "Recursively search for files by glob pattern, optionally filtered by content.", [
            ("path", "string", "Root search directory.", True),
            ("pattern", "string", "Glob pattern e.g. '*.py'.", True),
            ("content_search", "string", "Only return files containing this string.", False),
        ]),
    ]
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

def get_auth() -> dict:
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
    table = Table(title="[bold]Connect to Gemini[/bold]", border_style="cyan",
                  show_header=False, padding=(0, 2))
    table.add_column("#", style="cyan bold", justify="right")
    table.add_column("Option", style="bold white")
    table.add_column("Notes", style="dim")
    table.add_row("1", "⚡ Quick Connect",
                  "Opens AI Studio in browser → get free key → paste here → saved for future")
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
        "[bold cyan]⚡ Quick Connect[/bold cyan]\n\n"
        "[bold]Step 1[/bold] — Browser opens to Google AI Studio\n"
        "[bold]Step 2[/bold] — Sign in with your Google account\n"
        "[bold]Step 3[/bold] — Click [bold green]\"Create API key\"[/bold green] and copy it\n"
        "[bold]Step 4[/bold] — Paste it below\n\n"
        "[bold green]✓ Free[/bold green]   "
        "[bold green]✓ No credit card[/bold green]   "
        "[bold green]✓ No Cloud Console[/bold green]   "
        "[bold green]✓ No billing[/bold green]\n"
        "[dim]Key saved locally — future runs skip this step.[/dim]",
        border_style="cyan", padding=(0, 1)
    ))
    try:
        subprocess.Popen(
            ["powershell", "-Command", f"Start-Process '{AISTUDIO_URL}'"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        console.print(f"[dim]Browser opened → {AISTUDIO_URL}[/dim]")
    except Exception:
        console.print(f"[dim]Open manually: [bold]{AISTUDIO_URL}[/bold][/dim]")

    console.print()
    key = Prompt.ask("[bold]Paste your API key[/bold] (hidden)", password=True).strip()
    if not key:
        console.print("[red]No key entered. Exiting.[/red]")
        sys.exit(1)
    _save_key(key)
    console.print("[green]✅ Key saved — future runs will be instant![/green]\n")
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

def select_model(auth_mode: str) -> str:
    models = {
        "1": ("gemini-3.1-pro-preview",           "[bold magenta]Next Gen[/bold magenta] — Gemini 3.1 Pro Preview"),
        "2": ("gemini-3-pro-preview",             "[bold magenta]Next Gen[/bold magenta] — Gemini 3.0 Pro Preview"),
        "3": ("gemini-3-flash-preview",           "[bold magenta]Next Gen[/bold magenta] — Gemini 3.0 Flash Preview"),
        "4": ("gemini-2.5-pro",                   "[bold yellow]Flagship[/bold yellow] — reasoning & complex tasks"),
        "5": ("gemini-2.5-flash",                 "[bold green]Recommended[/bold green] — fast & smart"),
        "6": ("gemini-2.5-flash-lite",            "[cyan]Lightest[/cyan] — highest limits"),
        "7": ("gemini-2.0-flash-thinking-exp-01-21", "[bold purple]Thinking mode[/bold purple]"),
    }
    table = Table(title="[bold white]✨ GCLI Model Selection ✨[/bold white]", border_style="bright_blue",
                  show_header=True, header_style="bold cyan", padding=(0, 2))
    table.add_column("#", style="cyan bold", justify="right")
    table.add_column("Model ID", style="bright_white")
    table.add_column("Notes", style="dim")
    
    for k, (mid, note) in models.items():
        if k == "5":
            table.add_row(f"[bold bright_green]{k}[/bold bright_green]", f"[bold bright_green]{mid}[/bold bright_green]", note)
        else:
            table.add_row(k, mid, note)
            
    console.print()
    console.print(table)
    console.print("  [dim]↳ Or type any model ID (e.g. gemini-1.5-pro)[/dim]\n")
    
    choice = Prompt.ask("[bold bright_blue]❯ Model choice[/bold bright_blue]", default="5")
    if choice in models:
        mid, note = models[choice]
        console.print(f"[bright_green]✓[/bright_green] Initializing [bold cyan]{mid}[/bold cyan]...\n")
        return mid
        
    console.print(f"[bright_green]✓[/bright_green] Custom model: [bold cyan]{rich_escape(choice)}[/bold cyan]...\n")
    return choice

# ══════════════════════════════════════════════════════════════════════════════
#  GEMINI CLIENT  
# ══════════════════════════════════════════════════════════════════════════════

class GcliClient:
    def __init__(self, auth: dict, model_id: str):
        self._auth = auth
        self._bridge_proc = None
        if auth["mode"] == "oauth":
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
            self._sdk = genai.Client(api_key=auth["api_key"])
        self._model_id = model_id
        self._tools = _build_tools()
        
        # Tools JSON dump for the bridge
        self._bridge_tools = _build_bridge_tools()

        self._history: list = []

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
                temperature=0.3,
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

    def _step_with_retry_stream(self, live, max_retries: int = 5):
        wait = 5
        for attempt in range(max_retries):
            try:
                gen = self._bridge_generate_stream() if self._auth.get('mode') == 'oauth' else self._step_stream()
                for text, fc in gen:
                    yield text, fc
                return
            except Exception as e:
                err = str(e)
                is_rate_limit = "RESOURCE_EXHAUSTED" in err or "429" in err or "quota" in err.lower()
                if is_rate_limit and attempt < max_retries - 1:
                    if live.is_started:
                        live.stop()
                    
                    msg = f"[yellow]Got stuck, reloading... ({attempt + 1}/{max_retries - 1})[/yellow]"
                    if attempt == 0:
                        console.print(Panel(msg, title="[yellow]⏳ Rate Limited[/yellow]", border_style="yellow", padding=(0, 1)))
                    else:
                        console.print(f"  {msg}")
                    time.sleep(wait)
                else:
                    raise

    def send(self, user_text: str) -> None:
        self._history.append({"role": "user", "parts": [{"text": user_text}]})
        MAX_ROUNDS = 30
        for _round in range(MAX_ROUNDS):
            console.print()
            
            accumulated_text = ""
            function_calls = []
            
            from rich.live import Live
            live = Live(auto_refresh=True, refresh_per_second=10)
            try:
                for text_chunk, fc in self._step_with_retry_stream(live):
                    if text_chunk:
                        accumulated_text += text_chunk
                        if not live.is_started:
                            live.start()
                        live.update(Panel(Markdown(accumulated_text), title="[bold magenta]✨ GCLI[/bold magenta]", border_style="magenta", padding=(0, 1)))
                    if fc:
                        function_calls.append(fc)
                if live.is_started:
                    live.stop()
            except Exception as e:
                if self._history and self._history[-1]["role"] == "user":
                    self._history.pop()
                if live.is_started:
                    live.stop()
                raise
            
            if not accumulated_text.strip():
                # If no text was output at all, do not keep an empty panel.
                # Rich Live has already printed it though. We can't unprint easily, but it's fine.
                pass
                
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
                console.print(f"\n[bold blue]  🔧 Tool: [cyan]{fc_name}[/cyan][/bold blue]")
                if fc_name in TOOL_DISPATCH:
                    try:
                        result = TOOL_DISPATCH[fc_name](**fc_args)
                    except TypeError as e:
                        result = {"success": False, "error": f"Bad args: {e}"}
                else:
                    result = {"success": False, "error": f"Unknown tool: {fc_name}"}
                tool_response_parts.append({
                    "functionResponse": {"name": fc_name, "response": {"result": result}}
                })
            self._history.append({"role": "user", "parts": tool_response_parts})
            console.print("[dim italic]  ↩  Results sent to Gemini...[/dim italic]")
        else:
            console.print("[dim]⚠️  Reached max tool rounds (30).[/dim]")
# ══════════════════════════════════════════════════════════════════════════════
#  UI
# ══════════════════════════════════════════════════════════════════════════════

def print_banner():
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    console.print(Panel(
        f"[bold cyan]{BANNER}[/bold cyan]"
        f"[bold bright_cyan]The Autonomous Terminal AI[/bold bright_cyan]   [dim]Powered by Google Gemini[/dim]\n"
        f"\n"
        f"  [dim]cwd:[/dim]  [cyan]{rich_escape(_cwd)}[/cyan]\n"
        f"  [dim]time:[/dim] {now}",
        border_style="magenta", padding=(0, 2)
    ))


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    print_banner()
    auth = get_auth()
    model_id = select_model(auth["mode"])

    try:
        client = GcliClient(auth=auth, model_id=model_id)
    except Exception as e:
        console.print(f"[red]Failed to initialize: {e}[/red]")
        sys.exit(1)

    console.print(Panel(
        "[dim]GCLI ready. Type your task naturally.\n"
        "Commands: [bold]/help[/bold]  [bold]/history[/bold]  [bold]/model[/bold]  [bold]/clear[/bold]  "
        "[bold]exit[/bold][/dim]",
        border_style="dim", padding=(0, 1)
    ))

    while True:
        try:
            user_input = Prompt.ask("\n[bold bright_green]❯ You[/bold bright_green]")
            if not user_input.strip():
                continue

            low = user_input.strip().lower()

            if low in ["exit", "quit", "bye", ":q"]:
                console.print("\n[dim]Goodbye! 👋[/dim]\n")
                break

            if low == "/cwd":
                console.print(f"[cyan]cwd: {rich_escape(_cwd)}[/cyan]")
                continue

            if low == "/history":
                console.print(Panel("[bold cyan]Tool Execution Log[/bold cyan]", border_style="cyan"))
                for h in client._history:
                    if h["role"] == "model" and any("functionCall" in p for p in h["parts"]):
                        for p in h["parts"]:
                            if "functionCall" in p:
                                fc = p["functionCall"]
                                console.print(f"\n[bold magenta]AI Call:[/bold magenta] [cyan]{fc['name']}[/cyan] (args: {fc.get('args', {})})")
                    elif h["role"] == "user" and any("functionResponse" in p for p in h["parts"]):
                        for p in h["parts"]:
                            if "functionResponse" in p:
                                fr = p["functionResponse"]
                                res = str(fr['response']['result'])[:1000]
                                if len(str(fr['response']['result'])) > 1000: res += " ...[truncated]"
                                console.print(f"[bold green]Result:[/bold green] [dim]{rich_escape(res)}[/dim]")
                console.print()
                continue

            if low == "/clear":
                client._history.clear()
                console.clear()
                print_banner()
                console.print(f"[dim]History cleared. Using: [cyan]{client._model_id}[/cyan][/dim]")
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
                    model_id = new_model
                    client._model_id = new_model
                    console.print(f"[bright_green]✓[/bright_green] Model switched to [bold cyan]{new_model}[/bold cyan]")
                else:
                    # Show picker
                    model_id = select_model(auth["mode"])
                    client._model_id = model_id
                    console.print(f"[bright_green]✓[/bright_green] Now using [bold cyan]{model_id}[/bold cyan]")
                continue

            if low == "/forget-key":
                if SAVED_KEY_FILE.exists():
                    SAVED_KEY_FILE.unlink()
                    console.print("[green]✓ Saved key deleted. Next run will prompt for a new key.[/green]")
                else:
                    console.print("[dim]No saved key found.[/dim]")
                continue

            if low == "/switch-key":
                console.print("[dim]Enter your Gemini API key (from aistudio.google.com):[/dim]")
                new_key = _manual_key_flow()
                CONFIG_DIR.mkdir(parents=True, exist_ok=True)
                SAVED_KEY_FILE.write_text(new_key, encoding="utf-8")
                client._sdk = genai.Client(api_key=new_key)
                console.print("[green]✓ Switched to new API key and saved it.[/green]")
                continue

            if low == "/help":
                from rich.columns import Columns
                cmds = [
                    "[bold cyan]/model[/bold cyan] [dim]<id>[/dim]  Switch AI model mid-session",
                    "[bold cyan]/model[/bold cyan]        Show model picker",
                    "[bold cyan]/history[/bold cyan]      Show tool execution log",
                    "[bold cyan]/clear[/bold cyan]        Clear conversation & terminal",
                    "[bold cyan]/cwd[/bold cyan]          Show current directory",
                    "[bold cyan]/handoff[/bold cyan]      Toggle AI→AI task delegation",
                    "[bold cyan]/switch-key[/bold cyan]   Enter a new API key",
                    "[bold cyan]/forget-key[/bold cyan]   Delete saved API key",
                    "[bold cyan]/help[/bold cyan]         This help message",
                    "[bold cyan]exit[/bold cyan]          Quit GCLI",
                ]
                examples = [
                    "what files are in this folder?",
                    "create a Flask REST API with 3 endpoints",
                    "fix the bug in main.py",
                    "search the web for the latest numpy release",
                    "write a 3D snake game and run it",
                    "git status and summarize changes",
                ]
                console.print()
                console.print(Panel(
                    "\n".join(cmds) + "\n\n[bold]Examples:[/bold]\n" + "\n".join(f"  [dim]❯[/dim] {e}" for e in examples),
                    title="[bold bright_cyan]✨ GCLI Help[/bold bright_cyan]", border_style="cyan", padding=(0, 2)
                ))
                continue

            client.send(user_input)

        except KeyboardInterrupt:
            console.print("\n[dim]Interrupted. Ctrl+C again to exit.[/dim]")
            try:
                time.sleep(0.5)
            except KeyboardInterrupt:
                console.print("\n[dim]Goodbye! 👋[/dim]")
                break

        except Exception as e:
            err = str(e)
            # Check for common actionable errors
            if "API_KEY_INVALID" in err or "API key not valid" in err:
                console.print(Panel(
                    "[bold red]❌ Invalid API Key[/bold red]\n\n"
                    "Your API key was rejected.\n\n"
                    "Run [bold]/switch-key[/bold] to enter a valid key, or\n"
                    f"get a free key at: [bold]{AISTUDIO_URL}[/bold]",
                    title="[red]Auth Error[/red]", border_style="red"
                ))
            elif "NOT_FOUND" in err and "not found for API version" in err:
                console.print(Panel(
                    f"[bold yellow]⚠️  Model not available: [cyan]{rich_escape(model_id)}[/cyan][/bold yellow]\n\n"
                    "That model ID is not valid for the current API.\n\n"
                    "[bold]Working models:[/bold]\n"
                    "  gemini-2.0-flash            ← recommended\n"
                    "  gemini-2.0-flash-lite       ← highest free quota (30 RPM)\n"
                    "  gemini-2.5-pro-exp-03-25    ← most capable\n"
                    "  gemini-1.5-pro-latest       ← long context\n\n"
                    "Restart GCLI to choose a different model.",
                    title="[yellow]Model Not Found[/yellow]", border_style="yellow"
                ))
            elif "quota" in err.lower() or "RESOURCE_EXHAUSTED" in err:
                console.print(Panel(
                    "[bold yellow]⚠️  Rate limited — free tier quota exhausted[/bold yellow]\n\n"
                    "The Gemini CLI's API key has a shared free-tier quota.\n\n"
                    "[bold]Options:[/bold]\n"
                    "  1. Wait a minute and try again\n"
                    "  2. Type [bold cyan]/switch-key[/bold cyan] to use your own personal key\n"
                    "     (free at [bold]aistudio.google.com[/bold] — higher quota)\n"
                    "  3. Pick a higher-quota model: [cyan]gemini-2.0-flash-lite[/cyan] (30 RPM)",
                    title="[yellow]Rate Limited[/yellow]", border_style="yellow"
                ))
            else:
                console.print(f"\n[bold red]Error:[/bold red] {rich_escape(err)}")
                console.print(f"[dim]{rich_escape(traceback.format_exc())}[/dim]")
            console.print("[dim]You can continue or type 'exit'.[/dim]")


if __name__ == "__main__":
    main()
