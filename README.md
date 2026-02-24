# GCLI - The Autonomous Terminal AI

A Claude Code / Codex-style AI assistant for your terminal, powered by Google Gemini with native function calling.

## Prerequisites

- Python 3.10+
- Node.js (optional, for Gemini CLI integration)

## Quick Start

1. Install dependencies:
   ```powershell
   pip install -r requirements.txt
   ```
2. Optional Gemini CLI login:
   ```powershell
   npm install -g @google/gemini-cli
   gemini login
   ```
3. Run:
   ```powershell
   python gcli.py
   ```

## CLI Flags

```powershell
python gcli.py --help
python gcli.py --model gemini-2.5-flash-lite
python gcli.py --no-banner --model gemini-2.5-flash-lite --prompt "summarize this folder"
```

## Authentication

- Option A: API key from <https://aistudio.google.com/app/apikey>
- Option B: Gemini CLI integration (`gemini login`)

## Major Features (15+)

- Autonomous Gemini function-calling agent loop
- Persistent settings: `/settings`, `/set`, `/get`, `/reset-setting`
- Safe mode command guardrails (on by default)
- Session management: `/session save|load|list|export|import|new`
- Auto-session snapshots (`~/.gcli/sessions/autosave_latest.json`)
- Profiles: `/profile save|use|list|del`
- Aliases: `/alias add|list|del`
- Snippets with variables: `/snippet add|run|show|list|del`
- Macros: `/macro add|run|show|list|del`
- Bookmarks: `/bookmark add|go|list|del`
- Persistent notes: `/note add|list|del|clear|push`
- Persistent todos: `/todo add|list|done|undone|del|clear`
- Pinboard: `/pin add|last|list|del|clear`
- Undo/redo snapshots: `/undo`, `/redo`
- Transcript export: `/transcript [path]`
- Diagnostics and analytics: `/diag`, `/scan`
- Runtime stats and replay: `/status`, `/stats`, `/replay`, `/last`, `/retry`
- Directory permission manager: `/perm list|mode|trust|untrust|once|clear-once|check`
- Modernized CLI UI: cleaner banner, prompt, startup card, and command surfaces

## Utility Commands (20+)

- `/help`, `/cwd`, `/pwd`, `/clear`, `/history`, `/history N`
- `/perm list`, `/perm mode prompt|allow-all`, `/perm trust <path>`
- `/time`, `/date`, `/version`, `/session-id`, `/uptime`
- `/echo`, `/ls [pattern]`, `/tree [path]`
- `/find <text>`, `/trim <n>`, `/summary`
- `/clear-tools`, `/tag add|list|del|clear`
- `/handoff`, `/switch-key`, `/forget-key`, `exit`

## Tools Exposed to Gemini

- `run_command`
- `read_file`
- `write_file`
- `edit_file`
- `list_directory`
- `change_directory`
- `search_files`
- `delegate_task`
- `delete_file` (blocked by safe mode)
- `create_directory`
- `move_file`
- `search_web`
- `read_url`
