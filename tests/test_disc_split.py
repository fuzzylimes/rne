"""Tests for disc_split pure algorithmic functions."""

from __future__ import annotations

import pathlib

import pytest

from rne.cli.disc_split import (
    Episode,
    autodetect,
    fixed_split,
    groups_to_episodes,
)
from rne.cli._pipeline import _build_disc_split_jobs
from rne.models import AudioTrack, HandbrakeArgs
from rne.probe import Chapter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_chapters(durations: list[float]) -> list[Chapter]:
    """Build a Chapter list from a sequence of durations (seconds)."""
    chapters = []
    t = 0.0
    for i, d in enumerate(durations, start=1):
        chapters.append(Chapter(number=i, start=t, end=t + d, title=f"Chapter {i:02d}"))
        t += d
    return chapters


def _default_hb_args() -> HandbrakeArgs:
    return HandbrakeArgs(audio_tracks=[AudioTrack(track=1)])


# ---------------------------------------------------------------------------
# Episode dataclass
# ---------------------------------------------------------------------------


def test_episode_duration():
    chapters = _make_chapters([600.0, 840.0])
    ep = Episode(number=1, chapter_start=1, chapter_end=2, chapters=chapters)
    assert ep.duration == pytest.approx(1440.0)


def test_episode_duration_str_no_hours():
    chapters = _make_chapters([1200.0])
    ep = Episode(number=1, chapter_start=1, chapter_end=1, chapters=chapters)
    assert ep.duration_str() == "20:00"


def test_episode_duration_str_with_hours():
    chapters = _make_chapters([3661.0])
    ep = Episode(number=1, chapter_start=1, chapter_end=1, chapters=chapters)
    assert ep.duration_str() == "1:01:01"


# ---------------------------------------------------------------------------
# autodetect
# ---------------------------------------------------------------------------


def test_autodetect_even_split():
    # 4 chapters of 600s each, target 1200s → should produce 2 groups of 2
    chapters = _make_chapters([600.0, 600.0, 600.0, 600.0])
    groups = autodetect(chapters, 1200.0)
    assert len(groups) == 2
    assert len(groups[0]) == 2
    assert len(groups[1]) == 2


def test_autodetect_short_chapters_absorbed():
    # Simulate an anime disc: 4 × 1400s content chapters + 1 × 90s OP between each
    # Target is 1440s (24 min). OP chapters should be absorbed into episode groups.
    durations = [1400.0, 90.0, 1400.0, 90.0, 1400.0, 90.0, 1400.0]
    chapters = _make_chapters(durations)
    groups = autodetect(chapters, 1440.0)
    # Should produce roughly 4 groups (each ~1400–1490s), not 7
    assert len(groups) <= 5


def test_autodetect_orphan_folded_into_last():
    # 3 chapters of 1440s + 1 short leftover — orphan should fold into episode 3
    chapters = _make_chapters([1440.0, 1440.0, 1440.0, 60.0])
    groups = autodetect(chapters, 1440.0)
    assert len(groups) == 3
    assert len(groups[-1]) == 2  # last episode has the orphan folded in


def test_autodetect_empty():
    assert autodetect([], 1440.0) == []


def test_autodetect_single_chapter():
    chapters = _make_chapters([2000.0])
    groups = autodetect(chapters, 1440.0)
    assert len(groups) == 1
    assert len(groups[0]) == 1


# ---------------------------------------------------------------------------
# groups_to_episodes
# ---------------------------------------------------------------------------


def test_groups_to_episodes_chapter_offsets():
    chapters = _make_chapters([600.0] * 6)
    groups = [chapters[0:2], chapters[2:4], chapters[4:6]]
    episodes = groups_to_episodes(groups, start_ep=1)
    assert episodes[0].chapter_start == 1
    assert episodes[0].chapter_end == 2
    assert episodes[1].chapter_start == 3
    assert episodes[1].chapter_end == 4
    assert episodes[2].chapter_start == 5
    assert episodes[2].chapter_end == 6


def test_groups_to_episodes_custom_start_ep():
    chapters = _make_chapters([600.0] * 4)
    groups = [chapters[0:2], chapters[2:4]]
    episodes = groups_to_episodes(groups, start_ep=5)
    assert episodes[0].number == 5
    assert episodes[1].number == 6


# ---------------------------------------------------------------------------
# fixed_split
# ---------------------------------------------------------------------------


def test_fixed_split_even():
    chapters = _make_chapters([300.0] * 6)
    episodes = fixed_split(chapters, n=2, start_ep=1)
    assert len(episodes) == 3
    assert episodes[0].chapter_start == 1
    assert episodes[0].chapter_end == 2
    assert episodes[2].chapter_start == 5
    assert episodes[2].chapter_end == 6


def test_fixed_split_remainder_in_last():
    # 7 chapters split into groups of 3 → [3, 3, 1]
    chapters = _make_chapters([300.0] * 7)
    episodes = fixed_split(chapters, n=3, start_ep=1)
    assert len(episodes) == 3
    assert episodes[-1].chapter_start == 7
    assert episodes[-1].chapter_end == 7


# ---------------------------------------------------------------------------
# _build_disc_split_jobs
# ---------------------------------------------------------------------------


def test_build_disc_split_jobs_shared_source():
    chapters = _make_chapters([1440.0, 1440.0])
    groups = [chapters[0:1], chapters[1:2]]
    episodes = groups_to_episodes(groups, start_ep=3)
    source = pathlib.Path("/rips/disc.mkv")
    staging = pathlib.Path("/staging/ShowName")
    hb = _default_hb_args()

    jobs = _build_disc_split_jobs(episodes, source, "ShowName", 1, staging, hb)

    assert len(jobs) == 2
    for job in jobs:
        assert job["source_path"] == str(source)
        assert job["show"] == "ShowName"
        assert job["season"] == 1
        assert job["movie"] is None


def test_build_disc_split_jobs_per_episode_chapter_ranges():
    chapters = _make_chapters([1440.0] * 3)
    groups = [chapters[0:1], chapters[1:2], chapters[2:3]]
    episodes = groups_to_episodes(groups, start_ep=1)
    jobs = _build_disc_split_jobs(
        episodes,
        pathlib.Path("/src.mkv"),
        "Show",
        2,
        pathlib.Path("/out"),
        _default_hb_args(),
    )
    assert jobs[0]["handbrake_args"].chapter_start == 1
    assert jobs[0]["handbrake_args"].chapter_end == 1
    assert jobs[1]["handbrake_args"].chapter_start == 2
    assert jobs[1]["handbrake_args"].chapter_end == 2
    assert jobs[2]["handbrake_args"].chapter_start == 3
    assert jobs[2]["handbrake_args"].chapter_end == 3


def test_build_disc_split_jobs_output_paths():
    chapters = _make_chapters([1440.0, 1440.0])
    groups = [chapters[0:1], chapters[1:2]]
    episodes = groups_to_episodes(groups, start_ep=5)
    jobs = _build_disc_split_jobs(
        episodes,
        pathlib.Path("/src.mkv"),
        "Initial D",
        1,
        pathlib.Path("/staging/Initial D"),
        _default_hb_args(),
    )
    assert jobs[0]["output_path"].endswith("Initial D - S01E05.mkv")
    assert jobs[1]["output_path"].endswith("Initial D - S01E06.mkv")
    assert jobs[0]["label"] == "S01E05"
    assert jobs[1]["label"] == "S01E06"


def test_build_disc_split_jobs_encoding_config_propagated():
    chapters = _make_chapters([1440.0])
    episodes = groups_to_episodes([chapters], start_ep=1)
    hb = HandbrakeArgs(
        quality=18,
        preset="medium",
        tune="animation",
        audio_tracks=[AudioTrack(track=1)],
        decomb=True,
    )
    jobs = _build_disc_split_jobs(
        episodes,
        pathlib.Path("/src.mkv"),
        "Show",
        1,
        pathlib.Path("/out"),
        hb,
    )
    args = jobs[0]["handbrake_args"]
    assert args.quality == 18
    assert args.preset == "medium"
    assert args.tune == "animation"
    assert args.decomb is True
