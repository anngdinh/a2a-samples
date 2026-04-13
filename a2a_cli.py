#!/usr/bin/env python3
"""A2A Protocol CLI - Interactive terminal agent chat client."""

import asyncio
import os
import shutil
import sys
import time
from typing import Optional
from uuid import uuid4

import click
import httpx
from a2a.client import A2ACardResolver, ClientConfig, ClientFactory, create_text_message_object
from a2a.types import AgentCard, Message, Task, TransportProtocol
from a2a.utils.constants import EXTENDED_AGENT_CARD_PATH
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory
from prompt_toolkit.input import create_input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.styles import Style as PTStyle
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.spinner import Spinner
from rich.text import Text
from rich.theme import Theme

# ── Rich console ──────────────────────────────────────────────────────────────

theme = Theme({
    "info":  "dim",
    "error": "bold red",
})
console = Console(theme=theme, highlight=False)


# ── Input history ─────────────────────────────────────────────────────────────

HISTORY_FILE = os.path.expanduser("~/.a2a_cli_history")
_history = FileHistory(HISTORY_FILE)


# ── Agent card ────────────────────────────────────────────────────────────────

async def fetch_agent_card(base_url: str, auth_token: str = "") -> Optional[AgentCard]:
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as http:
        resolver = A2ACardResolver(httpx_client=http, base_url=base_url)
        card: Optional[AgentCard] = None

        try:
            card = await resolver.get_agent_card()
        except Exception:
            try:
                card = await resolver.get_agent_card(
                    relative_card_path="/.well-known/agent.json"
                )
            except Exception as e:
                raise RuntimeError(f"Could not fetch agent card: {e}") from e

        if card and card.supports_authenticated_extended_card and auth_token:
            try:
                extended = await resolver.get_agent_card(
                    relative_card_path=EXTENDED_AGENT_CARD_PATH,
                    http_kwargs={"headers": {"Authorization": f"Bearer {auth_token}"}},
                )
                extended.url = base_url
                return extended
            except Exception:
                pass

        if card:
            card.url = base_url
        return card


# ── Send / stream ─────────────────────────────────────────────────────────────

class Session:
    """Holds conversation state across turns."""

    def __init__(self, agent_card: AgentCard, auth_token: str = "") -> None:
        self.agent_card = agent_card
        self.auth_token = auth_token
        self.context_id: str = str(uuid4())
        self.task_id: Optional[str] = None

    def reset(self) -> None:
        self.context_id = str(uuid4())
        self.task_id = None


async def _stream_once(session: Session, text: str) -> str:
    """Send *text* to the agent, stream status updates, return collected answer."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as http:
        if session.auth_token:
            http.headers["Authorization"] = f"Bearer {session.auth_token}"

        config = ClientConfig(
            httpx_client=http,
            supported_transports=[TransportProtocol.jsonrpc],
        )
        client = ClientFactory(config).create(session.agent_card)

        msg = create_text_message_object(content=text)
        msg.message_id = uuid4().hex
        msg.context_id = session.context_id
        if session.task_id:
            msg.task_id = session.task_id

        lines: list[str] = []
        working_lines: list[str] = []
        seen_artifacts: set[str] = set()
        seen_status_ids: set[str] = set()

        spinner = Spinner("dots", text="  Thinking…", style="dim")

        with Live(spinner, console=console, refresh_per_second=12, transient=True):
            async for event in client.send_message(msg):
                if isinstance(event, Message):
                    for part in event.parts:
                        if hasattr(part.root, "text"):
                            lines.append(part.root.text)
                            working_lines.append(part.root.text)
                            spinner.update(text=Text(f"  → {part.root.text}", style="dim italic"))
                    if hasattr(event, "context_id") and event.context_id:
                        session.context_id = event.context_id
                    if hasattr(event, "task_id") and event.task_id:
                        session.task_id = event.task_id

                elif isinstance(event, tuple) and len(event) >= 1:
                    task: Task = event[0]

                    if hasattr(task, "context_id") and task.context_id:
                        session.context_id = task.context_id
                    if hasattr(task, "id") and task.id:
                        session.task_id = task.id

                    # Print new artifacts (skip already-seen — SDK accumulates).
                    new_artifact_text = False
                    if task.artifacts:
                        for artifact in task.artifacts:
                            aid = getattr(artifact, "artifact_id", None) or id(artifact)
                            if aid in seen_artifacts:
                                continue
                            seen_artifacts.add(aid)
                            for part in artifact.parts:
                                if hasattr(part.root, "text"):
                                    new_artifact_text = True
                                    lines.append(part.root.text)

                    # Status message — only when no new artifacts.
                    if task.status and task.status.message and not new_artifact_text:
                        msg_id = getattr(task.status.message, "message_id", None) or id(task.status.message)
                        if msg_id not in seen_status_ids:
                            seen_status_ids.add(msg_id)
                            for part in task.status.message.parts:
                                if hasattr(part.root, "text"):
                                    state = task.status.state.value if task.status.state else ""
                                    is_final = state in ("completed", "input-required")
                                    lines.append(part.root.text)
                                    if is_final:
                                        pass  # will be printed after Live stops
                                    else:
                                        working_lines.append(part.root.text)
                                        spinner.update(text=Text(f"  → {part.root.text}", style="dim italic"))

        # ── Print output after spinner is cleared ──
        # Working steps (dim)
        for wl in working_lines:
            if wl.strip():
                console.print(f"  [dim italic]→ {wl}[/dim italic]")

        # Final answer(s) rendered as markdown
        for line in lines:
            if line.strip() and line not in working_lines:
                md = Markdown(line, code_theme="monokai")
                console.print(md)

        return "\n\n".join(lines)


# ── Help & banner ─────────────────────────────────────────────────────────────

COMMANDS = {
    "/new":        "New conversation",
    "/quit":       "Exit",
    "Alt+Enter":   "New line",
}

def print_banner(card: AgentCard) -> None:
    name    = card.name or "Agent"
    version = getattr(card, "version", "") or ""
    desc    = card.description or ""
    url     = card.url or ""

    title = Text(name, style="bold cyan")
    if version:
        title.append(f"  v{version}", style="dim")

    subtitle_parts = []
    if desc:
        subtitle_parts.append(desc)
    if url:
        subtitle_parts.append(url)
    subtitle = Text("\n".join(subtitle_parts), style="dim") if subtitle_parts else None

    console.print()
    console.print(Panel(
        "  ".join(f"[dim green]{cmd}[/]" for cmd in COMMANDS),
        title=title,
        subtitle=subtitle,
        border_style="dim",
        padding=(0, 2),
    ))
    console.print()



# ── Bordered input box ────────────────────────────────────────────────────────

_PT_STYLE = PTStyle.from_dict({
    "border": "fg:ansibrightcyan",
    "prompt": "fg:ansibrightgreen bold",
})

_session = PromptSession(history=_history, erase_when_done=True)


def _get_prompt() -> FormattedText:
    width = shutil.get_terminal_size().columns
    return FormattedText([
        ("class:border", "─" * width + "\n"),
        ("class:prompt", "❯ "),
    ])


def _prompt_continuation(width: int, line_number: int, wrap_count: int) -> FormattedText:
    return FormattedText([("class:prompt", "  ")])


async def read_input() -> str:
    """Top-border input. Enter=submit, Alt+Enter=newline. Paste multi-line freely."""
    kb = KeyBindings()

    @kb.add("enter")
    def _submit(event):
        event.current_buffer.validate_and_handle()

    @kb.add("escape", "enter")   # Alt+Enter
    def _newline(event):
        event.current_buffer.insert_text("\n")

    @kb.add("escape")
    def _clear(event):
        event.current_buffer.reset()

    @kb.add("c-c")
    def _interrupt(event):
        event.current_buffer.reset()

    @kb.add("c-d")
    def _eof(event):
        if not event.current_buffer.text:
            event.app.exit(exception=EOFError())

    return await _session.prompt_async(
        _get_prompt,
        multiline=True,
        key_bindings=kb,
        style=_PT_STYLE,
        prompt_continuation=_prompt_continuation,
    )


# ── ESC-to-cancel ─────────────────────────────────────────────────────────────

async def _wait_for_esc() -> None:
    """Resolve as soon as ESC is pressed."""
    inp = create_input()
    esc = asyncio.Event()

    def _check():
        for kp in inp.read_keys():
            if kp.key == Keys.Escape:
                esc.set()

    with inp.raw_mode():
        with inp.attach(_check):
            await esc.wait()


# ── Main REPL ─────────────────────────────────────────────────────────────────

async def repl(agent_url: str, auth_token: str) -> None:
    # Fetch agent card
    console.print(f"\n  [info]Connecting to {agent_url} …[/info]")
    try:
        card = await fetch_agent_card(agent_url, auth_token)
    except Exception as e:
        console.print(f"\n  [error]Error: {e}[/error]\n")
        sys.exit(1)

    if card is None:
        console.print("\n  [error]Could not fetch agent card. Is the server running?[/error]\n")
        sys.exit(1)

    session = Session(card, auth_token)
    print_banner(card)

    while True:
        try:
            user_input = (await read_input()).strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            break

        if not user_input:
            continue

        # ── built-in commands ──────────────────────────────────────────────
        cmd = user_input.lower().split()[0]
        if cmd in ("/quit", "/exit", "/q"):
            break

        if cmd == "/new":
            session.reset()
            console.print("\n  [dim]New conversation started.[/dim]\n")
            continue

        # ── echo input in plain text, then send to agent (ESC cancels) ───────
        console.print(f"[bold green]❯ {user_input}[/bold green]")
        console.print()
        start = time.monotonic()

        async def _run(text: str) -> str:
            try:
                return await _stream_once(session, text)
            except Exception as e:
                err = str(e)
                if "terminal state" in err or "completed" in err or "does not exist" in err:
                    session.task_id = None
                    return await _stream_once(session, text)
                raise

        stream_task = asyncio.create_task(_run(user_input))
        esc_task    = asyncio.create_task(_wait_for_esc())

        done, _ = await asyncio.wait(
            {stream_task, esc_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        # Cancel whichever is still running
        for t in (stream_task, esc_task):
            if not t.done():
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass

        if esc_task in done:
            session.task_id = None
            console.print("\n  [dim]Cancelled.[/dim]\n")
            continue

        try:
            answer = stream_task.result()
        except Exception as e:
            console.print(f"\n  [error]Error: {e}[/error]\n")
            continue

        elapsed = time.monotonic() - start
        if not answer.strip():
            answer = "(no text response)"

        console.print()
        console.print(f"  [dim]⏱  {elapsed:.2f}s[/dim]")
        console.print()

    console.print("\n  [dim]Bye.[/dim]\n")


# ── CLI entry point ───────────────────────────────────────────────────────────

@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("agent_url", default="", required=False)
@click.option("--token", "-t", default="", envvar="A2A_TOKEN",
              help="Bearer token for authenticated agents (or set A2A_TOKEN env var).")
def main(agent_url: str, token: str) -> None:
    """Interactive CLI for A2A protocol agents.

    \b
    Examples:
      uv run a2a_cli.py http://localhost:10000
      uv run a2a_cli.py http://localhost:10000 --token my-secret
      A2A_TOKEN=my-secret uv run a2a_cli.py http://localhost:10000
    """
    if not agent_url:
        try:
            agent_url = input("\001\033[1m\033[36m\002  Agent URL: \001\033[0m\002").strip()
        except (EOFError, KeyboardInterrupt):
            sys.exit(0)
        if not agent_url:
            click.echo("No URL provided.", err=True)
            sys.exit(1)

    asyncio.run(repl(agent_url, token))


if __name__ == "__main__":
    main()
