# GCLI — The Autonomous Terminal AI

A Claude Code / Codex-style AI assistant for your terminal, powered by **Google Gemini** with native function calling.

## Prerequisites

- **Python 3.10+**
- **Node.js** (optional, for Gemini CLI integration)

## Quick Start

1. Install Python dependencies:
   ```powershell
   pip install -r requirements.txt
   ```

2. (Optional) Install the official Gemini CLI for seamless login:
   ```powershell
   npm install -g @google/gemini-cli
   gemini login
   ```
   *This allows GCLI to piggyback on your Gemini CLI session without needing an API key.*

3. Run GCLI:
   ```powershell
   python gcli.py
   ```

---

## Authentication

GCLI supports two authentication methods:

### 🔑 Option A: API Key (Standard)

1. Get a free API key from **[Google AI Studio](https://aistudio.google.com/app/apikey)**.
2. Run GCLI and paste the key when prompted.
3. The key is saved locally to `~/.gcli/apikey.txt`.

### 🔗 Option B: Gemini CLI Integration (No API Key)

If you have the **Gemini CLI** installed and logged in (`gemini login`), GCLI will automatically detect your session and use it. This avoids copy-pasting API keys and uses the same OAuth credentials as the official CLI.

---

## Features

| Feature | Details |
|---------|---------|
| **Autonomous tool use** | Gemini calls tools directly — no markdown parsing hacks |
| **File operations** | read, write, edit, search |
| **Shell commands** | PowerShell execution |
| **Agentic loop** | Up to 30 tool-call rounds per message |
| **Session history** | Context maintained across messages |

## Available Tools

| Tool | Description |
|------|-------------|
| `run_command` | Execute PowerShell commands |
| `read_file` | Read file contents |
| `write_file` | Create or overwrite files |
| `edit_file` | Surgical find-and-replace |
| `list_directory` | Browse directory structure |
| `change_directory` | Change working directory |
| `search_files` | Recursive file search |
| `delegate_task` | Spawn sub-agents for parallel tasks |

## Slash Commands

| Command | Action |
|---------|--------|
| `/help` | Show help |
| `/cwd` | Show current working directory |
| `/clear` | Clear conversation history |
| `/handoff` | Toggle AI task delegation |
| `/switch-key` | Enter a new API key |
| `/forget-key` | Delete saved API key |
| `exit` | Quit GCLI |
