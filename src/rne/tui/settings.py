"""The track and encode settings form, shared by ingest, queue and edit.

Choices come from the source's real tracks (rne.plan); this module only lays
them out and reads them back.
"""

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Button, Checkbox, Label, Select


from rne import config, plan
from rne.models import HandbrakeArgs
from rne.probe import StreamSummary
from rne.tui.common import Plain, plain
from rne.tui.nav import NavRow, NavSelect

_NO_TUNE = "none"  # Select can't hold None as a value


def _with_current(options: list[tuple[str, object]], current: object, label: str) -> list:
    """Make sure the job's current value is selectable even if it's unusual."""
    if current is not None and current not in [v for _, v in options]:
        options.append((f"{label} (current)", current))
    return options


class AudioRowWidget(NavRow):
    """One source audio track: include, codec, bitrate, move up/down."""

    DEFAULT_CSS = """
    AudioRowWidget { height: auto; }
    AudioRowWidget Checkbox { width: 1fr; }
    AudioRowWidget.excluded Checkbox { color: $text-muted; }
    AudioRowWidget.excluded Select { display: none; }
    AudioRowWidget .codec { width: 14; margin-left: 1; }
    AudioRowWidget .bitrate { width: 16; margin-left: 1; }
    AudioRowWidget Button { min-width: 3; width: 3; margin-left: 1; }
    """

    def __init__(self, row: plan.AudioRow) -> None:
        super().__init__()
        self.row = row

    def compose(self) -> ComposeResult:
        r = self.row
        self.set_class(not r.include, "excluded")
        yield Checkbox(plain(r.label), r.include, classes="include", compact=True)
        codecs = _with_current([(c, c) for c in plan.AUDIO_CODECS], r.codec, r.codec)
        yield NavSelect(codecs, value=r.codec, allow_blank=False, compact=True, classes="codec")
        bitrate = r.bitrate or r.recommended_bitrate
        rates = [
            (f"{b}k" + (" (rec.)" if b == r.recommended_bitrate else ""), b)
            for b in plan.AUDIO_BITRATES
        ]
        rates = _with_current(rates, bitrate, f"{bitrate}k")
        yield NavSelect(
            rates, value=bitrate, allow_blank=False, compact=True,
            classes="bitrate", disabled=r.codec == "copy",
        )
        yield Button("▲", classes="up", compact=True, tooltip="Move up")
        yield Button("▼", classes="down", compact=True, tooltip="Move down")

    def read(self) -> plan.AudioRow:
        codec = self.query_one(".codec", Select).value
        bitrate = self.query_one(".bitrate", Select).value
        return plan.AudioRow(
            track=self.row.track,
            label=self.row.label,
            include=self.query_one(".include", Checkbox).value,
            codec=str(codec),
            bitrate=None if codec == "copy" else int(bitrate),  # type: ignore[arg-type]
            recommended_bitrate=self.row.recommended_bitrate,
        )

    @on(Checkbox.Changed, ".include")
    def _include_changed(self, event: Checkbox.Changed) -> None:
        self.set_class(not event.value, "excluded")

    @on(Select.Changed, ".codec")
    def _codec_changed(self, event: Select.Changed) -> None:
        self.query_one(".bitrate", Select).disabled = event.value == "copy"

    @on(Button.Pressed, ".up")
    def _up(self, event: Button.Pressed) -> None:
        event.stop()
        self.move(-1)

    @on(Button.Pressed, ".down")
    def _down(self, event: Button.Pressed) -> None:
        event.stop()
        self.move(1)

    def move(self, step: int) -> None:
        parent = self.parent
        assert parent is not None
        siblings = list(parent.query_children(AudioRowWidget))
        i = siblings.index(self)
        j = i + step
        if not 0 <= j < len(siblings):
            return
        if step < 0:
            parent.move_child(self, before=siblings[j])
        else:
            parent.move_child(self, after=siblings[j])
        self.post_message(SettingsForm.Changed())


class SubtitleRowWidget(NavRow):
    """One source subtitle track: include and default."""

    DEFAULT_CSS = """
    SubtitleRowWidget { height: auto; }
    SubtitleRowWidget .include { width: 1fr; }
    SubtitleRowWidget.excluded .include { color: $text-muted; }
    SubtitleRowWidget .default { width: 12; }
    """

    class DefaultChosen(Message):
        def __init__(self, track: int) -> None:
            super().__init__()
            self.track = track

    def __init__(self, row: plan.SubtitleRow) -> None:
        super().__init__()
        self.row = row

    def compose(self) -> ComposeResult:
        self.set_class(not self.row.include, "excluded")
        yield Checkbox(plain(self.row.label), self.row.include, classes="include", compact=True)
        yield Checkbox("default", self.row.default, classes="default", compact=True)

    def read(self) -> plan.SubtitleRow:
        return plan.SubtitleRow(
            track=self.row.track,
            label=self.row.label,
            include=self.query_one(".include", Checkbox).value,
            default=self.query_one(".default", Checkbox).value,
        )

    @on(Checkbox.Changed, ".default")
    def _default_changed(self, event: Checkbox.Changed) -> None:
        if event.value:
            self.query_one(".include", Checkbox).value = True
            self.post_message(self.DefaultChosen(self.row.track))

    @on(Checkbox.Changed, ".include")
    def _include_changed(self, event: Checkbox.Changed) -> None:
        self.set_class(not event.value, "excluded")
        if not event.value:
            self.query_one(".default", Checkbox).value = False


class SettingsForm(Vertical):
    """Audio, subtitle and encode settings for one source layout."""

    DEFAULT_CSS = """
    SettingsForm { height: auto; }
    SettingsForm Vertical { height: auto; }
    SettingsForm .section { text-style: bold; margin-top: 1; color: $accent; }
    SettingsForm .hint { color: $text-muted; }
    SettingsForm .field { height: auto; }
    SettingsForm .field Label { width: 16; }
    SettingsForm .field Select { width: 24; }
    SettingsForm .field Checkbox { width: 24; }
    SettingsForm .reason { color: $text-muted; padding-left: 2; }
    """

    class Changed(Message):
        """Any setting changed; listeners re-validate."""

    def __init__(
        self,
        summary: StreamSummary | None,
        args: HandbrakeArgs,
        *,
        is_dvd: bool = False,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self.summary = summary
        self.base = args
        self.is_dvd = is_dvd

    def compose(self) -> ComposeResult:
        args = self.base
        # None: the source can't be read; show the job's own settings only.
        known = self.summary or StreamSummary(video=[], audio=[], subtitle=[])
        dvd_source = plan.is_dvd_source(known, self.is_dvd)

        yield Plain("Audio", classes="section")
        yield Plain("Tick tracks to keep. The first kept track plays by default; ▲▼ reorder.",
                     classes="hint")
        with Vertical(id="audio-rows"):
            for row in plan.audio_rows(self.summary, args):
                yield AudioRowWidget(row)

        yield Plain("Subtitles", classes="section")
        sub_rows = plan.subtitle_rows(self.summary, args)
        if sub_rows:
            yield Plain("Few frames usually means a forced-only track.", classes="hint")
        elif self.summary is None:
            yield Plain("No subtitle tracks chosen.", classes="hint")
        else:
            yield Plain("No subtitle tracks in the source.", classes="hint")
        with Vertical(id="sub-rows"):
            for row in sub_rows:
                yield SubtitleRowWidget(row)

        yield Plain("Encode", classes="section")
        qualities = [
            (f"{q}" + (" (default)" if q == config.DEFAULT_QUALITY else ""), q)
            for q in range(0, 52)
        ]
        with Horizontal(classes="field"):
            yield Label("Quality (CRF)")
            yield NavSelect(qualities, value=args.quality, allow_blank=False, compact=True, id="quality")
            yield Plain("lower = better quality, bigger file", classes="reason")
        default_preset = plan.default_preset(dvd_source)
        presets = [
            (p + (" (default)" if p == default_preset else ""), p) for p in plan.ENCODER_PRESETS
        ]
        with Horizontal(classes="field"):
            yield Label("Preset")
            yield NavSelect(_with_current(presets, args.preset, args.preset),
                         value=args.preset, allow_blank=False, compact=True, id="preset")
            yield Plain("slower = smaller file at the same quality", classes="reason")
        tunes = [(t or _NO_TUNE, t or _NO_TUNE) for t in plan.ENCODER_TUNES]
        current_tune = args.tune or _NO_TUNE
        with Horizontal(classes="field"):
            yield Label("Tune")
            yield NavSelect(_with_current(tunes, current_tune, current_tune),
                         value=current_tune, allow_blank=False, compact=True, id="tune")
            yield Plain("animation for cartoons and anime", classes="reason")
        detelecine = plan.detelecine_option(known, dvd_source)
        with Horizontal(classes="field"):
            yield Label("Detelecine")
            yield Checkbox("", args.detelecine, id="detelecine", compact=True)
            yield Plain(f"recommended on: {detelecine.reason}" if detelecine else "",
                         classes="reason")
        decomb = plan.decomb_option(known, dvd_source)
        with Horizontal(classes="field"):
            yield Label("Decomb")
            yield Checkbox("", args.decomb, id="decomb", compact=True)
            yield Plain(
                f"recommended {'on' if decomb.default else 'off'}: {decomb.reason}"
                if decomb else "",
                classes="reason",
            )

    # -- reading ---------------------------------------------------------------

    def value(self) -> HandbrakeArgs:
        """Current settings. Raises ValueError if a row can't form a track."""
        tune = self.query_one("#tune", Select).value
        return plan.args_from_rows(
            self.base,
            [w.read() for w in self.query(AudioRowWidget)],
            [w.read() for w in self.query(SubtitleRowWidget)],
            quality=int(self.query_one("#quality", Select).value),  # type: ignore[arg-type]
            preset=str(self.query_one("#preset", Select).value),
            tune=None if tune == _NO_TUNE else str(tune),
            detelecine=self.query_one("#detelecine", Checkbox).value,
            decomb=self.query_one("#decomb", Checkbox).value,
        )

    def errors(self) -> list[str]:
        try:
            return plan.validate(self.value(), self.summary)
        except ValueError as exc:
            return [str(exc)]

    # -- change tracking ---------------------------------------------------------

    @on(SubtitleRowWidget.DefaultChosen)
    def _one_default(self, event: SubtitleRowWidget.DefaultChosen) -> None:
        for w in self.query(SubtitleRowWidget):
            if w.row.track != event.track:
                box = w.query_one(".default", Checkbox)
                if box.value:
                    box.value = False

    @on(Checkbox.Changed)
    @on(Select.Changed)
    def _changed(self) -> None:
        self.post_message(self.Changed())
