"""Styles and small widgets shared by the rne terminal apps."""

import sys

from rich.text import Text
from textual.content import Content
from textual.widgets import Static


def clear_after_app() -> None:
    """Clear the terminal once a full-screen app has exited.

    Textual draws on the terminal's alternate screen, so normally the shell
    comes back as it was. GNU screen (alternate screen off by default) and
    some SSH clients leave the app's last frame behind instead; clearing
    makes the summary that follows land at the top of a clean terminal.
    Only for a real terminal, so piped output never gets escape codes.
    """
    if sys.stdout.isatty():
        sys.stdout.write("\033[H\033[2J")  # cursor home, clear screen (scrollback kept)
        sys.stdout.flush()


class Plain(Static):
    """Static that shows its text literally.

    Textual reads "[...]" as style markup, which would swallow things like a
    "[eng]" language tag, "[tvdb]" in a message, or brackets in a file name.
    """

    def __init__(self, content: str = "", **kwargs) -> None:
        kwargs.setdefault("markup", False)
        super().__init__(content, **kwargs)


def plain(text: object) -> Content:
    """A widget label shown literally (see Plain)."""
    return Content(str(text))


def cell(text: object) -> Text:
    """A DataTable cell shown literally. (A Content cell isn't measured, so
    its column collapses to the header's width.)"""
    return Text(str(text))

APP_CSS = """
Screen { background: $surface; }
.section { text-style: bold; margin-top: 1; color: $accent; }
.hint { color: $text-muted; }
.field { height: auto; }
.field Label { width: 16; }
.field Input { width: 50; }
.field Input.number { width: 8; }
.field Input { background: $boost; }
.field Input:focus { background: $primary 30%; }
#errors { color: $error; margin-top: 1; }
#outputs { color: $text-muted; }
#buttons { height: auto; margin-top: 1; }
#buttons Button { margin-right: 2; }
#body { padding: 0 2; }
.muted { color: $text-muted; }
.ok { color: $success; }
.warn { color: $warning; }
DataTable { height: auto; max-height: 20; }
"""
