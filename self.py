# uv run self.py [step | history]

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Self, cast

import rtoml
from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam, ChatCompletionToolParam
from openai.lib.streaming.chat import ChunkEvent, ContentDeltaEvent
from dotenv import load_dotenv
from pydantic import BaseModel, Field, TypeAdapter


def _tool_terminal_output() -> tuple[str, str]:
    # currently not used as a tool but as input.
    try:
        result = subprocess.run(
            ["zellij", "action", "dump-screen"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return "", result.stderr.strip()
        return result.stdout.strip(), ""
    except FileNotFoundError:
        return "", "zellij not found"
    except subprocess.TimeoutExpired:
        return "", "zellij timeout"
    except Exception as e:
        return "", f"zellij error: {e}"


def _tool_terminal_input(sequence: str, *, timeout=10) -> str:
    try:
        result = subprocess.run(
            ["zellij", "action", "write-chars", "--", sequence],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return "ok" if result.returncode == 0 else f"terminal error: {result.stderr.strip()}"
    except FileNotFoundError:
        return "terminal not found, current tool is broken"
    except subprocess.TimeoutExpired:
        return "terminal timeout"
    except Exception as e:
        return f"terminal exception: {e}"


DEFAULT_SELF = Path(__file__).parent / "SELF.md"
DEFAULT_SYSTEM = (
    DEFAULT_SELF.read_text()
    if DEFAULT_SELF.exists()
    else (
        "You are controlling the terminal via input, "
        "and each round of input is the terminal screen. "
        "The current terminal content:"
    )
)
TOOLSET: dict[str, Callable[..., str]] = {"input": _tool_terminal_input}
TOOLS: list[ChatCompletionToolParam] = [
    {
        "type": "function",
        "function": {
            "name": "input",
            "description": (
                "Write characters into the terminal. Ordinary characters type "
                "literally; \\n = Enter, \\x03 = Ctrl+C, \\t = Tab. "
                "Without \\n, a shell command is only typed, not run.\n\n"
                "Examples:\n"
                "  Shell: 'ls\\n'     runs command (Enter)\n"
                "  Vim:   'dd'       deletes a line\n"
                "  Vim:   '/foo\\n'   searches (Enter confirms)\n\n"
                "Control keys: "
                "\\n (Enter) | "
                "\\x03 (Ctrl+C) | "
                "\\x04 (Ctrl+D) | "
                "\\x0c (Ctrl+L) | "
                "\\x15 (Ctrl+U) | "
                "\\x18 (Ctrl+X) | "
                "\\t (Tab)"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sequence": {
                        "type": "string",
                        "description": (
                            "Characters to write. Use \\n for Enter, \\xNN for Ctrl+key, \\t for Tab."
                        ),
                    },
                },
                "required": ["sequence"],
            },
        },
    },
]


class ToolCallFunction(BaseModel):
    name: str
    arguments: str


class ToolCall(BaseModel):
    id: str
    type: str = "function"
    function: ToolCallFunction


class Message(BaseModel):
    role: str
    content: str | None = None
    reasoning_content: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None

    def to_chat_completion_message(self) -> ChatCompletionMessageParam:
        return cast(ChatCompletionMessageParam, self.model_dump(exclude_none=True))


class Session(BaseModel):
    messages: list[Message] = Field(default_factory=list)
    context_tokens: int | None = None

    @classmethod
    def default(cls) -> Self:
        return cls(messages=[Message(role="system", content=DEFAULT_SYSTEM)])

    @classmethod
    def load(cls, path: Path) -> Self:
        raw = rtoml.load(path)
        return cls(
            messages=TypeAdapter(list[Message]).validate_python(raw.get("messages", [])),
            context_tokens=raw.get("context_tokens"),
        )

    def save(self, path: Path) -> None:
        swap = path.with_suffix(".tmp")
        swap.write_text(rtoml.dumps(self.model_dump(exclude_none=True), pretty=True))
        swap.rename(path)

    def to_chat_completion_messages(self) -> Iterable[ChatCompletionMessageParam]:
        return (m.to_chat_completion_message() for m in self.messages)


class Tagged:
    def __init__(self) -> None:
        self.tag: str | None = None

    def reasoning(self, text: str) -> str:
        return self._delta("[THINK] ", text)

    def content(self, text: str) -> str:
        return self._delta("[REPLY] ", text)

    def _delta(self, tag: str, text: str) -> str:
        if self.tag == tag:
            return text
        prefix = "" if self.tag is None else "\n"
        self.tag = tag
        return prefix + tag + text

    @staticmethod
    def tool_call(t: ToolCall) -> str:
        return f"[CALL {t.id}] {t.function.name}({_flatten_json(t.function.arguments)})"

    @staticmethod
    def message(m: Message) -> str:
        match m.role:
            case "assistant":
                output = []
                if m.reasoning_content:
                    output.append(f"[THINK] {m.reasoning_content}")
                if m.content:
                    output.append(f"[REPLY] {m.content}")
                for t in m.tool_calls or []:
                    output.append(Tagged.tool_call(t))
                return "\n".join(output)

            case "tool":
                return f"[TOOL {m.tool_call_id}] {m.content}"

            case "user":
                return f"[INPUT] {m.content}"

            case "system":
                return f"[SYSTEM] {m.content}"

            case _:
                return f"[{m.role}] {m.content}"


def _hr() -> None:
    print("-" * 40)


def _flatten_json(raw: str) -> str:
    try:
        args = json.loads(raw)
        if isinstance(args, dict):
            return ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in args.items())
    except json.JSONDecodeError:
        pass
    return raw


def _call_tool(name: str, args: str) -> str:
    fn = TOOLSET.get(name)
    if not fn:
        return f"unknown tool name {name}"
    try:
        return fn(**json.loads(args))
    except Exception as e:
        return str(e)


def show(session: Session) -> None:
    for message in session.messages:
        _hr()
        print(Tagged.message(message))


def chat(client: OpenAI, model: str, session: Session, content: str) -> None:
    messages = [Message(role="user", content=content)]
    while messages:
        _hr()
        for message in messages:
            print(Tagged.message(message))

        session.messages.extend(messages)
        with client.chat.completions.stream(
            messages=session.to_chat_completion_messages(),
            model=model,
            tools=TOOLS,
            stream_options={"include_usage": True},
        ) as stream:
            _hr()
            tagged = Tagged()
            for event in stream:
                match event:
                    case ContentDeltaEvent():
                        if delta := event.delta:
                            print(tagged.content(delta), end="", flush=True)
                    case ChunkEvent():
                        delta = event.chunk.choices[0].delta
                        if r := getattr(delta, "reasoning_content", None):
                            print(tagged.reasoning(str(r)), end="", flush=True)
                    case _:
                        pass
            print()

        final = stream.get_final_completion()
        message = final.choices[0].message
        message = Message(
            role="assistant",
            content=message.content,
            reasoning_content=getattr(message, "reasoning_content", None),
            tool_calls=[
                ToolCall(
                    id=t.id,
                    type=t.type,
                    function=ToolCallFunction(name=t.function.name, arguments=t.function.arguments),
                )
                for t in message.tool_calls
            ]
            if message.tool_calls
            else None,
        )

        if final.usage:
            session.context_tokens = final.usage.total_tokens
        session.messages.append(message)
        for t in message.tool_calls or []:
            print(Tagged.tool_call(t))

        messages = [
            Message(
                role="tool",
                tool_call_id=t.id,
                content=_call_tool(t.function.name, t.function.arguments),
            )
            for t in message.tool_calls or []
        ]


def _build_user_message(session: Session, screen: str) -> str:
    total = session.context_tokens
    return f"""\
time: {datetime.now().isoformat()}
context-tokens: {total if total is not None else "unknown"}
terminal-screen:
{screen}
"""


def main() -> None:
    if not load_dotenv():
        load_dotenv(Path(__file__).parent / ".env")

    history = os.getenv("SESSION", "session.toml")
    history = Path(__file__).with_name(history)
    session = Session.load(history) if history.exists() else Session.default()

    api_key = os.getenv("OAI_API_KEY", "")
    api_url = os.getenv("OAI_API_URL", "https://api.deepseek.com")
    client = OpenAI(api_key=api_key, base_url=api_url)

    model = os.getenv("OAI_MODEL", "deepseek-v4-pro")
    match sys.argv[1:]:
        case [] | ["step"]:
            match _tool_terminal_output():
                case screen, "":
                    content = _build_user_message(session, screen)
                    chat(client, model, session, content)
                    session.save(history)
                case "", error:
                    print(error)
                    sys.exit(1)
                case _:
                    print("unexpected error")
                    sys.exit(1)

        case ["history"]:
            show(session)

        case _:
            print("usage: self.py [step | history]")
            sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
