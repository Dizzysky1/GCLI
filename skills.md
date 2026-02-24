# GCLI — System Prompt / Skills Reference
# (This file is no longer loaded at runtime — the system prompt is embedded in gcli.py)
# Kept here as a reference document.

## What GCLI Can Do

GCLI uses **native Gemini Function Calling** to autonomously operate on your machine.

### Available Tools
| Tool | Description |
|------|-------------|
| `run_command` | Execute PowerShell commands |
| `read_file` | Read file contents |
| `write_file` | Create/overwrite files |
| `edit_file` | Surgical find-and-replace edits |
| `list_directory` | Browse directory structure |
| `change_directory` | Change working directory |
| `search_files` | Recursive file search |
| `delegate_task` | (If /handoff enabled) spawn a sub-agent to perform complex logic |

### Long Tasks & Planning
GCLI is capable of handling complex, multi-step tasks by creating its own local checklist (`gcli_plan.md`). 
It updates this file as it progresses, checking off components and correcting itself if it runs into an unrecoverable error loops.

### Example Tasks
- "Create a React app in ./my-app"
- "Fix the bug in app.py"
- "What's in this directory? Explain the project structure."
- "Install black and format all Python files in ./src"
- "Write a Dockerfile for this project"
- "Add error handling to the login function in auth.js"
- "Build a full stack Next.js app, following the plan you lay out in gcli_plan.md"
