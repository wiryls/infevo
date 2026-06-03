import codecs
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import ClassVar, Iterable, Self, cast

import rtoml
from openai import (
    APIError,
    APIConnectionError,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    OpenAI,
    RateLimitError,
)
from openai.types.chat import ChatCompletionMessageParam, ChatCompletionToolParam
from openai.lib.streaming.chat import ChunkEvent, ContentDeltaEvent
from pydantic import BaseModel, Field, TypeAdapter, ValidationError


@dataclass
class Ok[T]:
    value: T


@dataclass
class Error:
    value: str


type Result[T] = Ok[T] | Error


@dataclass
class TerminalSnapshot:
    size: tuple[int, int]
    cursor: tuple[int, int]
    screen: str


@dataclass
class TerminalContext:
    name: str | None = None
    pane: int | None = None
    timeout: int = 5

    def _build_action(self, action: str, *args: str) -> list[str]:
        has_pane = action not in ["list-panes"]
        cmd = ["zellij"]
        cmd += ["-s", self.name] if self.name else []
        cmd += ["action", action]
        cmd += ["-p", str(self.pane)] if self.pane and has_pane else []
        cmd += args
        return cmd

    def _run_action(self, action: str, *args: str) -> Result[str]:
        command = self._build_action(action, *args)
        result = subprocess.run(command, capture_output=True, text=True, timeout=self.timeout)
        if result.returncode == 0:
            return Ok(result.stdout.strip())
        return Error(result.stderr.strip())

    class _PaneInfo(BaseModel):
        id: int
        is_plugin: bool
        is_focused: bool
        pane_rows: int
        pane_columns: int
        cursor_coordinates_in_pane: tuple[int, int] | None

    def snapshot(self) -> Result[TerminalSnapshot]:
        def _match(i: TerminalContext._PaneInfo) -> bool:
            return not i.is_plugin and (i.is_focused if self.pane is None else i.id == self.pane)

        try:
            match self._run_action("list-panes", "-j"), self._run_action("dump-screen"):
                case Ok(text), Ok(view):
                    panes = TypeAdapter(list[TerminalContext._PaneInfo]).validate_json(text)
                    pane = next(p for p in panes if _match(p))
                case Error(_) as error, _:
                    return error
                case _, Error(_) as error:
                    return error
        except FileNotFoundError:
            return Error("zellij communication error")
        except subprocess.TimeoutExpired:
            return Error("zellij action timeout")
        except StopIteration:
            return Error(f"zellij pane {str(self.pane)} not found")
        except ValidationError as e:
            return Error(f"zellij response malformed: {e}")

        size = (pane.pane_rows, pane.pane_columns)
        cursor = pane.cursor_coordinates_in_pane or (0, 0)
        return Ok(TerminalSnapshot(size=size, cursor=(cursor[1], cursor[0]), screen=view))

    def input(self, sequence: str, *, paste: bool = False, unescape: bool = False) -> str:
        try:
            action = "paste" if paste else "write-chars"
            sequence = codecs.decode(sequence, "unicode_escape") if unescape else sequence
            match self._run_action(action, "--", sequence):
                case Ok(_):
                    return "sent"
                case Error(error):
                    return f"terminal error: {error}"
        except FileNotFoundError:
            return "terminal not found, current tool is broken"
        except subprocess.TimeoutExpired:
            return "terminal timeout"
        except UnicodeDecodeError as e:
            return f"terminal unescape error: {e}"

    _SCROLL_ACTIONS: ClassVar[dict[tuple[str, bool], str]] = {
        ("line", True): "scroll-up",
        ("line", False): "scroll-down",
        ("half-page", True): "half-page-scroll-up",
        ("half-page", False): "half-page-scroll-down",
        ("page", True): "page-scroll-up",
        ("page", False): "page-scroll-down",
        ("end", True): "scroll-to-top",
        ("end", False): "scroll-to-bottom",
    }

    def scroll(self, method: str, *, up: bool = True) -> str:
        action = self._SCROLL_ACTIONS.get((method, up))
        if action is None:
            return f"error: unknown method '{method}'"
        try:
            match self._run_action(action):
                case Ok(_):
                    return "sent"
                case Error(error):
                    return f"scroll error: {error}"
        except FileNotFoundError:
            return "terminal not found, current tool is broken"
        except subprocess.TimeoutExpired:
            return "terminal timeout"

    def call(self, name: str, arguments: str) -> str:
        fn = {"input": self.input, "scroll": self.scroll}.get(name)
        if not fn:
            return f"unknown tool: {name}"
        try:
            return fn(**json.loads(arguments))
        except json.JSONDecodeError as e:
            return f"tool call JSON malformed: {e}"
        except TypeError as e:
            return f"tool call argument mismatch: {e}"

    TOOLS: ClassVar[list[ChatCompletionToolParam]] = [
        {
            "type": "function",
            "function": {
                "name": "input",
                "description": r"""Type characters at the terminal cursor.

Set paste=true for bracketed paste mode.

Set unescape=true to interpret escapes sequences, e.g.:
  \n  = Enter
  \t  = Tab
  \b  = Backspace
  \f  = Ctrl+L
  \\  = literal backslash

Support JSON escapes, e.g.:
  \u001b    = ESC
  \u0003    = Ctrl+C
  \u0004    = Ctrl+D
  \u0018    = Ctrl+X
  \u001b[A  = Up
  \u001b[B  = Down
  \u001b[5~ = PageUp
  \u001b[6~ = PageDown

Examples of unescape=true:
  git commit -m "msg"  # shell: type without executing
  ls -la\n             # shell: type and run
  ihello\u001b:wq\n    # vim: insert, ESC, save-quit
  \u0003               # Ctrl+C to interrupt

In shell, bash $'...' handles escapes natively, e.g.:
  printf $'\e[31mred\e[0m'\n

This tool is asynchronous, returns 'sent' if command sent to terminal else error messages.""",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "sequence": {
                            "type": "string",
                            "description": "Characters to write.",
                        },
                        "paste": {
                            "type": "boolean",
                            "description": "Send as a bracketed paste instead of typing character by character.",
                        },
                        "unescape": {
                            "type": "boolean",
                            "description": "Interpret escape sequences (e.g. \\u0003 -> Ctrl+C).",
                        },
                    },
                    "required": ["sequence"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "scroll",
                "description": (
                    "Scroll the terminal viewport to review historical output. "
                    "It does NOT send keystrokes to the terminal program, only repositions your view.\n\n"
                    "After scrolling, `input` will auto-scroll the viewport back to the cursor; "
                    "you do NOT need to manually scroll back before typing.\n\n"
                    "This tool is asynchronous, returns 'sent' if sent to terminal else error messages."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "method": {
                            "type": "string",
                            "enum": ["line", "half-page", "page", "end"],
                            "description": "Scroll method: line, half-page, page, or end (up=true to top; false to bottom).",
                        },
                        "up": {
                            "type": "boolean",
                            "description": "True to scroll up (see earlier output), false to scroll down.",
                        },
                    },
                    "required": ["method"],
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
    rounds: int = 0

    @classmethod
    def empty(cls, prompt: str) -> Self:
        return cls(messages=[Message(role="system", content=prompt)])

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


def show(session: Session) -> None:
    for message in session.messages:
        _hr()
        print(Tagged.message(message))


def chat(client: OpenAI, model: str, session: Session, tool: TerminalContext, content: str) -> None:
    session.rounds += 1
    messages = [Message(role="user", content=content)]
    while messages:
        _hr()
        for message in messages:
            print(Tagged.message(message))

        session.messages.extend(messages)
        with client.chat.completions.stream(
            messages=session.to_chat_completion_messages(),
            model=model,
            tools=tool.TOOLS,
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
                content=tool.call(t.function.name, t.function.arguments),
            )
            for t in message.tool_calls or []
        ]


def save_sessions(session: Session, filepath: Path) -> None:
    _hr()
    try:
        session.save(filepath)
    except (OSError, TypeError, ValueError) as e:
        print(f"[SESSION] {e}")
    else:
        total_messages = len(session.messages)
        total_tokens = session.context_tokens
        print(f"[SESSION] {filepath} ({total_messages} messages, {total_tokens} tokens)")


class Conf(BaseModel):
    class Terminal(BaseModel):
        pane_id: int | None = None
        session_name: str | None = None
        command_timeout: int = 10

    class Provider(BaseModel):
        url: str = "https://api.deepseek.com"
        key: str = ""
        model: str = "deepseek-v4-pro"
        context_limit: int | None = None

    session: str = "session.toml"
    provider: Provider = Field(default_factory=Provider)
    terminal: Terminal = Field(default_factory=Terminal)

    @classmethod
    def load(cls, path: Path | None = None) -> "Conf":
        if path is None:
            name = "conf.toml"
            plan = (Path(name), Path(__file__).with_name(name))
            path = next(filter(Path.exists, plan), None)

        if path:
            text = rtoml.load(path) if path.exists() else {}
            conf = cls.model_validate(text)
        else:
            conf = Conf()
        return conf


def _build_user_message(session: Session, conf: Conf, snap: TerminalSnapshot) -> str:
    used = session.context_tokens
    total = conf.provider.context_limit
    context_tokens = "unknown" if used is None else f"{used}/{total}" if total else str(used)
    return f"""\
time: {datetime.now().isoformat(timespec="seconds")}
context-tokens: {context_tokens}
terminal-size: {snap.size}
terminal-cursor: {snap.cursor}
terminal-screen:
{snap.screen}
"""


def main() -> None:
    config = Conf.load()
    history = Path(__file__).with_name(config.session)
    system = Path(__file__).with_name("SELF.md")
    system_fallback = (
        "I'm controlling the terminal via `input`. "
        "Each round of user message is the terminal snapshot. "
        "Do whatever I want! \n"
        "Now, this is the current terminal:"
    )
    prompt = system.read_text() if system.exists() else system_fallback
    session = Session.load(history) if history.exists() else Session.empty(prompt)

    # setup tools
    tools = TerminalContext(
        name=config.terminal.session_name,
        pane=config.terminal.pane_id,
        timeout=config.terminal.command_timeout,
    )

    # setup client
    client = OpenAI(api_key=config.provider.key or None, base_url=config.provider.url)

    # start
    model = config.provider.model
    match sys.argv[1:]:
        case []:
            match tools.snapshot():
                case Ok(snap):
                    content = _build_user_message(session, config, snap)
                    try:
                        chat(client, model, session, tools, content)
                        save_sessions(session, history)
                    except (
                        AuthenticationError,
                        BadRequestError,
                        RateLimitError,
                        APIConnectionError,
                        InternalServerError,
                        APIError,
                    ) as e:
                        print(f"[LLM ERROR] {e}")
                        sys.exit(1)

                case Error(error):
                    print(error)
                    sys.exit(1)

        case ["history"]:
            show(session)

        case _:
            print("usage: self.py [history]")
            sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
