# INF-EVO

A terminal-native AI agent. No chat, no UI — just a shell, a screen, and a loop.

It doesn't do things for you — **it burns your tokens to do whatever it wants**.

## Prerequisites

### Install dependencies

1. Install [uv](https://github.com/astral-sh/uv) and [zellij](https://github.com/zellij-org/zellij).
2. `uv sync` to download Python dependencies.
3. `zellij -s <name>` to set up a session for the agent.

### Configure API credentials

Copy the example and fill in your keys:

```bash
cp .env.example .env
```

Setup `.env`:

```env
OAI_API_URL=https://api.deepseek.com
OAI_API_KEY=sk-your-key-here
OAI_MODEL=deepseek-v4-pro
```

Most OpenAI-compatible providers should work.

## Usage

```bash
# Wake the agent — it reads the terminal, thinks, and acts
uv run self.py step

# View the conversation history
uv run self.py history

# Let it run continuously (stops on failure)
while uv run self.py step; do sleep 1; done
```

Each `step` appends to `session.toml`. The agent's identity is defined in `SELF.md` — edit it to change its behavior.

### How it works

```
terminal screen → LLM (with SELF.md as system prompt) → characters → terminal
                       ↑                                               │
                       └──────────── session.toml ←────────────────────┘
```

The agent has only one tool: `input` — writes characters directly into the terminal. It can run shell commands, edit files in vim, navigate TUI programs, or do anything a user could type.

## Disclaimer

This agent has unrestricted access to your terminal. It may make mistakes, run wrong commands, and occasionally do things you don't want — that's not a bug, it's what happens when you hand an LLM a shell. Run it in a sandbox, a container, or a machine you don't care about.
