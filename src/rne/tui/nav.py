"""Arrow-key navigation for the rne forms.

- ↑/↓ move between fields (FORM_BINDINGS on each screen).
- Dropdowns open with Enter or Space, so ↑/↓ pass over them (NavSelect).
- ←/→ change a choice such as TV/Movie directly (NavRadioSet).
- In a list of rows (track lists), ↑/↓ move between rows in the same column,
  ←/→ along the row, and ↑/↓ past the ends continue to the fields
  around the list (NavRow).

Widgets that need the arrows themselves (tables, text inputs' ←/→, an open
dropdown) keep them: their bindings are checked before these.
"""

from __future__ import annotations

from textual.actions import SkipAction
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widget import Widget
from textual.widgets import RadioButton, RadioSet, Select

# The footer can't show ↑/↓ (text inputs claim those keys, silently), so
# form screens show this line instead.
NAV_HINT = "↑↓ move between fields · ←→ along a row or change a choice · Enter opens a list"

FORM_BINDINGS = [
    Binding("down", "app.focus_next", "Move", key_display="↑↓"),
    Binding("up", "app.focus_previous", "Move", show=False),
]


class NavSelect(Select):
    """A dropdown that lets ↑/↓ move to the next field; Enter/Space open it."""

    BINDINGS = [
        Binding("up", "pass_arrow", show=False),
        Binding("down", "pass_arrow", show=False),
    ]

    def action_pass_arrow(self) -> None:
        raise SkipAction()


class NavRadioSet(RadioSet):
    """←/→ pick the previous/next option; ↑/↓ move to the next field."""

    BINDINGS = [
        Binding("up", "pass_arrow", show=False),
        Binding("down", "pass_arrow", show=False),
        Binding("left", "pick(-1)", show=False),
        Binding("right", "pick(1)", show=False),
    ]

    def action_pass_arrow(self) -> None:
        raise SkipAction()

    def action_pick(self, step: int) -> None:
        buttons = list(self.query(RadioButton))
        if not buttons:
            return
        current = self.pressed_index if self.pressed_index >= 0 else 0
        target = (current + step) % len(buttons)
        buttons[target].value = True
        self._selected = target


def _usable(widget: Widget) -> bool:
    return widget.focusable and widget.display


class NavRow(Horizontal):
    """One row of a list: ↑/↓ between rows (same column), ←/→ along the row."""

    BINDINGS = [
        Binding("up", "row(-1)", show=False),
        Binding("down", "row(1)", show=False),
        Binding("left", "column(-1)", show=False),
        Binding("right", "column(1)", show=False),
    ]

    def controls(self) -> list[Widget]:
        return [w for w in self.children if _usable(w)]

    def _focused_child(self) -> Widget | None:
        focused = self.screen.focused
        while focused is not None and focused.parent is not self:
            focused = focused.parent  # type: ignore[assignment]
        return focused

    def action_column(self, step: int) -> None:
        controls = self.controls()
        current = self._focused_child()
        if current not in controls:
            return
        target = controls.index(current) + step
        if 0 <= target < len(controls):
            controls[target].focus()

    def action_row(self, step: int) -> None:
        current = self._focused_child()
        if current is None or self.parent is None:
            return
        rows = [r for r in self.parent.children if isinstance(r, NavRow)]
        target_index = rows.index(self) + step
        if 0 <= target_index < len(rows):
            # Same column if it can take focus there, else the nearest one before it.
            column = list(self.children).index(current)
            target = rows[target_index]
            candidates = [w for w in list(target.children)[: column + 1] if _usable(w)]
            (candidates[-1] if candidates else target.controls()[0]).focus()
            return
        # Past the first/last row: carry on to the field before/after the list.
        chain = self.screen.focus_chain
        mine = [w for w in chain if self in w.ancestors_with_self]
        if not mine:
            return
        edge = chain.index(mine[0]) - 1 if step < 0 else chain.index(mine[-1]) + 1
        if 0 <= edge < len(chain):
            chain[edge].focus()
