# Mini Cursor

Mini Cursor is a terminal-based coding agent. It can inspect a workspace, search
Python symbols, propose file changes for your approval, run commands in a
Podman container, and optionally search the web with Tavily.

## Requirements

- Python 3.10 or later
- Podman installed and available as `podman` on your `PATH` (needed for the
  agent's `run_command` tool)
- An OpenAI API key
- A Tavily API key only if you want to use web search

## Setup

From this directory, create and activate a virtual environment, then install
the Python dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install openai python-dotenv
```

Create a `.env` file in this directory:

```dotenv
OPENAI_API_KEY=your-openai-api-key

# Optional: the default model is gpt-5.4-mini
OPENAI_MODEL=gpt-5.4-mini

# Optional: use a compatible API endpoint
# OPENAI_API_BASE_URL=https://your-api-endpoint

# Optional: enables the web_search tool
# TAVILY_API_KEY=your-tavily-api-key
```

Keep `.env` private; do not commit API keys.

## Usage

Start an interactive session, using the current directory as the workspace:

```powershell
python main.py
```

Set a workspace explicitly:

```powershell
python main.py --workspace "C:\path\to\project"
```

Ask a question and exit after the response:

```powershell
python main.py --workspace "C:\path\to\project" "Explain the project structure"
```

In interactive mode, enter `new` to start a fresh conversation, `undo` to
revert the last approved file change, or `exit` to quit. The agent asks for
approval before writing or editing files and before running `pytest` through
its command tool.

## Agent tools

- List, read, and find workspace files
- Search Python class and function names
- Edit a file with an exact-text replacement or write a complete file
- Run commands inside a Podman container based on `python:3.12-slim` by default
- Search the web with Tavily when `TAVILY_API_KEY` is configured

The workspace is mounted into the container at `/workspace`. You can override
the container image with `MINI_CURSOR_CONTAINER_IMAGE`.

## Tests

Run the test suite with Python's built-in unittest runner:

```powershell
python -m unittest discover -v
```

Alternatively, install pytest and run:

```powershell
python -m pip install pytest
pytest -q
```
