"""The dashboard's edit form: one job or a whole batch.

Reads a submitted form into the same jobs.plan_edits call the terminal editor
makes, and keeps what was typed so a form with errors can be shown again.
Pure apart from the targets it is given: no Flask, no DB.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field, replace
from typing import Mapping, Protocol

from rne import config, jobs, naming, plan
from rne.models import HandbrakeArgs
from rne.probe import StreamSummary

NO_TUNE = "none"
KEEP_TVDB = "keep"


class FormData(Protocol):
    """werkzeug's MultiDict, or anything with get/getlist."""

    def get(self, key: str, default: str | None = None) -> str | None: ...
    def getlist(self, key: str) -> list[str]: ...


def _int_or_none(text: str | None) -> int | None:
    text = (text or "").strip()
    return int(text) if text.lstrip("-").isdigit() else None


@dataclass
class Choice:
    value: str
    label: str


@dataclass
class EditForm:
    targets: list[jobs.EditTarget]
    title: str
    year: str
    season: str
    episode: str
    move: bool
    library: str
    tvdb_id: str  # KEEP_TVDB, or the id of a match picked on this form
    tvdb_title: str  # the name that match set, to notice a later rename
    audio: list[plan.AudioRow]
    subtitles: list[plan.SubtitleRow]
    quality: int
    preset: str
    tune: str  # NO_TUNE for none
    detelecine: bool
    decomb: bool
    # Where the form started, so untouched fields keep each job's own value.
    initial_title: str = ""
    initial_move: bool = False
    initial_library: str = ""
    parse_errors: list[str] = field(default_factory=list)

    # -- facts about the jobs ------------------------------------------------------

    @property
    def first(self) -> jobs.EditTarget:
        return self.targets[0]

    @property
    def is_tv(self) -> bool:
        return self.first.is_tv

    @property
    def batch(self) -> bool:
        return len(self.targets) > 1

    @property
    def job_ids(self) -> list[int]:
        return [t.job_id for t in self.targets]

    @property
    def summary(self) -> StreamSummary | None:
        """Tracks come from the first readable source."""
        return _summary(self.targets)

    @property
    def unreadable(self) -> list[jobs.EditTarget]:
        return [t for t in self.targets if t.summary is None]

    @property
    def base(self) -> HandbrakeArgs:
        return self.first.args

    # -- choices for the selects ---------------------------------------------------

    def _dvd(self) -> bool:
        return plan.is_dvd_source(self.summary or _NO_SOURCE)

    def quality_choices(self) -> list[Choice]:
        return _with_current([
            Choice(str(q), f"{q}" + (" (default)" if q == config.DEFAULT_QUALITY else ""))
            for q in range(0, 52)
        ], str(self.quality))

    def preset_choices(self) -> list[Choice]:
        default = plan.default_preset(self._dvd())
        return _with_current([
            Choice(p, p + (" (default)" if p == default else "")) for p in plan.ENCODER_PRESETS
        ], self.preset)

    def tune_choices(self) -> list[Choice]:
        return _with_current(
            [Choice(t or NO_TUNE, t or NO_TUNE) for t in plan.ENCODER_TUNES], self.tune
        )

    @staticmethod
    def codec_choices(row: plan.AudioRow) -> list[Choice]:
        return _with_current([Choice(c, c) for c in plan.AUDIO_CODECS], row.codec)

    @staticmethod
    def bitrate_choices(row: plan.AudioRow) -> list[Choice]:
        current = row.bitrate or row.recommended_bitrate
        return _with_current([
            Choice(str(b), f"{b}k" + (" (rec.)" if b == row.recommended_bitrate else ""))
            for b in plan.AUDIO_BITRATES
        ], str(current))

    def detelecine_reason(self) -> str:
        opt = plan.detelecine_option(self.summary or _NO_SOURCE, self._dvd())
        return f"Recommended on: {opt.reason}" if opt else ""

    def decomb_reason(self) -> str:
        opt = plan.decomb_option(self.summary or _NO_SOURCE, self._dvd())
        if opt is None:
            return ""
        return f"Recommended {'on' if opt.default else 'off'}: {opt.reason}"

    # -- reading it back -----------------------------------------------------------

    def settings(self) -> HandbrakeArgs:
        """The chosen settings. Raises ValueError for an impossible track row."""
        return plan.args_from_rows(
            self.base, self.audio, self.subtitles,
            quality=self.quality, preset=self.preset,
            tune=None if self.tune == NO_TUNE else self.tune,
            detelecine=self.detelecine, decomb=self.decomb,
        )

    def plan(self) -> tuple[list[jobs.EditResult], list[str]]:
        """Each job's new naming and settings, plus everything that blocks saving."""
        errors = list(self.parse_errors)
        title = self.title.strip()
        if not title:
            errors.append("Name can't be empty.")
        year = _int_or_none(self.year)
        if self.year.strip() and (year is None or not 1880 <= year <= 2100):
            errors.append("Year must be a 4-digit year, or blank.")
        season = episode = None
        if self.is_tv:
            season, episode = _int_or_none(self.season), _int_or_none(self.episode)
            if season is None or season < 0:
                errors.append("Season must be 0 or more.")
            if episode is None or episode < 1:
                errors.append("Episode must be 1 or more.")
        try:
            template = self.settings()
        except ValueError as exc:
            errors.append(str(exc))
            template = None
        library = self._library_choice(errors)
        if errors:
            return [], errors
        results = jobs.plan_edits(
            self.targets, template=template, title=title, season=season,
            first_episode=episode, year=year, tvdb_id=self._tvdb_choice(title),
            library=library,
        )
        for r in results:
            errors += r.errors
        return results, errors

    def _tvdb_choice(self, title: str) -> object:
        """A match picked here; KEEP while the name is unchanged; else None."""
        picked = _int_or_none(self.tvdb_id)
        if picked is not None:
            return picked if title == self.tvdb_title.strip() else None
        return jobs.KEEP if title == self.initial_title else None

    def _library_choice(self, errors: list[str]) -> object:
        """KEEP each job's own destination, None (don't move), or one folder for all."""
        text = self.library.strip()
        if self.move == self.initial_move and text == self.initial_library:
            return jobs.KEEP
        if not self.move:
            return None
        folder = pathlib.Path(text).expanduser()
        if not text or not folder.is_absolute():
            errors.append("Library folder must be an absolute path.")
            return jobs.KEEP
        return folder

    # -- building ------------------------------------------------------------------

    @classmethod
    def from_targets(
        cls, targets: list[jobs.EditTarget], library: config.LibraryConfig | None = None
    ) -> EditForm:
        """The form as it first appears: the jobs' current values."""
        library = library or config.LibraryConfig()
        first = targets[0]
        args = first.args
        if first.final_path:
            lib = str(naming.library_dir_of(first.final_path, is_tv=first.is_tv))
        else:
            root = library.root_for(first.is_tv)
            lib = str(root / naming.folder_name(first.title, first.year)) if root else ""
        move = any(t.final_path for t in targets)
        summary = _summary(targets)
        return cls(
            targets=targets,
            title=first.title,
            year="" if first.year is None else str(first.year),
            season="" if first.season is None else str(first.season),
            episode=str(min(t.episode or 0 for t in targets)) if first.is_tv else "",
            move=move,
            library=lib,
            tvdb_id=KEEP_TVDB,
            tvdb_title="",
            audio=plan.audio_rows(summary, args),
            subtitles=plan.subtitle_rows(summary, args),
            quality=args.quality,
            preset=args.preset,
            tune=args.tune or NO_TUNE,
            detelecine=args.detelecine,
            decomb=args.decomb,
            initial_title=first.title,
            initial_move=move,
            initial_library=lib,
        )

    @classmethod
    def from_form(cls, targets: list[jobs.EditTarget], data: FormData | Mapping) -> EditForm:
        """The form as submitted. Unreadable values become parse errors, not exceptions."""
        get = data.get
        getlist = data.getlist  # type: ignore[union-attr]
        first = targets[0]
        summary = _summary(targets)
        errors: list[str] = []

        # Rows the form may refer to: every source track plus the job's own.
        known_audio = {r.track: r for r in plan.audio_rows(summary, first.args)}
        included = set(getlist("audio_include"))
        audio: list[plan.AudioRow] = []
        seen: set[int] = set()
        for raw in getlist("audio_order"):
            track = _int_or_none(raw)
            if track is None or track not in known_audio or track in seen:
                continue
            seen.add(track)
            row = known_audio[track]
            codec = (get(f"audio_codec_{track}") or row.codec).strip()
            bitrate = _int_or_none(get(f"audio_bitrate_{track}"))
            include = str(track) in included
            if codec != "copy" and (bitrate is None or bitrate <= 0):
                if include:
                    errors.append(f"Audio track {track}: pick a bitrate.")
                bitrate = row.recommended_bitrate
            audio.append(plan.AudioRow(
                track=track, label=row.label, include=include,
                codec=codec, bitrate=None if codec == "copy" else bitrate,
                recommended_bitrate=row.recommended_bitrate,
            ))
        # A row missing from the submission is shown again, unticked.
        audio += [replace(r, include=False) for t, r in known_audio.items() if t not in seen]

        sub_included = set(getlist("sub_include"))
        sub_default = (get("sub_default") or "").strip()
        subtitles = [
            plan.SubtitleRow(
                track=r.track, label=r.label,
                include=str(r.track) in sub_included or str(r.track) == sub_default,
                default=str(r.track) == sub_default,
            )
            for r in plan.subtitle_rows(summary, first.args)
        ]

        quality = _int_or_none(get("quality"))
        if quality is None:
            errors.append("Quality must be a number.")
            quality = first.args.quality
        return cls(
            targets=targets,
            title=(get("title") or "").strip(),
            year=(get("year") or "").strip(),
            season=(get("season") or "").strip(),
            episode=(get("episode") or "").strip(),
            move=bool(get("move")),
            library=(get("library") or "").strip(),
            tvdb_id=(get("tvdb_id") or KEEP_TVDB).strip(),
            tvdb_title=(get("tvdb_title") or "").strip(),
            audio=audio,
            subtitles=subtitles,
            quality=quality,
            preset=(get("preset") or first.args.preset).strip(),
            tune=(get("tune") or NO_TUNE).strip(),
            detelecine=bool(get("detelecine")),
            decomb=bool(get("decomb")),
            initial_title=(get("initial_title") or "").strip(),
            initial_move=get("initial_move") == "1",
            initial_library=(get("initial_library") or "").strip(),
            parse_errors=errors,
        )


_NO_SOURCE = StreamSummary(video=[], audio=[], subtitle=[])


def _summary(targets: list[jobs.EditTarget]) -> StreamSummary | None:
    return next((t.summary for t in targets if t.summary is not None), None)


def _with_current(choices: list[Choice], current: str) -> list[Choice]:
    """Keep the job's current value selectable even if it's unusual."""
    if current and current not in [c.value for c in choices]:
        choices.append(Choice(current, f"{current} (current)"))
    return choices
