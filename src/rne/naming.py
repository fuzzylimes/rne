"""Output file and folder names. Pure: builds paths, never touches the disk.

Templates (see SPEC.md "Storage layout"):

- TV:    {root}/{show}/Season {season:02d}/{show} - S{season:02d}E{episode:02d}.mkv
- Movie: {root}/{movie}/{movie}{suffix}.mkv
"""

from __future__ import annotations

import pathlib


def episode_label(season: int, episode: int) -> str:
    return f"S{season:02d}E{episode:02d}"


def title_dir(root: pathlib.Path, title: str) -> pathlib.Path:
    """The per-show / per-movie folder under a staging or library root."""
    return root / title


def tv_output_path(
    show_dir: pathlib.Path, show: str, season: int, episode: int
) -> pathlib.Path:
    return show_dir / f"Season {season:02d}" / f"{show} - {episode_label(season, episode)}.mkv"


def movie_output_path(
    movie_dir: pathlib.Path, movie: str, suffix: str = ""
) -> pathlib.Path:
    """suffix tells apart several files queued under one movie name, e.g. '_t03'."""
    return movie_dir / f"{movie}{suffix}.mkv"
