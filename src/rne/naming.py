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


def renamed_output(
    old_output: str | pathlib.Path,
    *,
    is_tv: bool,
    old_title: str,
    new_title: str,
    season: int | None = None,
    episode: int | None = None,
) -> pathlib.Path:
    """Where a job's output goes after its name, season or episode is edited.

    Stays under the same root. The show/movie folder is renamed only if it
    was named after the title; a custom staging folder chosen at rip time is
    kept. A movie's disambiguating suffix (e.g. '_t03') is kept.
    """
    old = pathlib.Path(old_output)
    if is_tv:
        if season is None or episode is None:
            raise ValueError("TV jobs need a season and an episode")
        show_dir = old.parent.parent
        if show_dir.name == old_title:
            show_dir = show_dir.parent / new_title
        return tv_output_path(show_dir, new_title, season, episode)
    movie_dir = old.parent
    suffix = old.stem[len(old_title):] if old.stem.startswith(old_title) else ""
    if movie_dir.name == old_title:
        movie_dir = movie_dir.parent / new_title
    return movie_output_path(movie_dir, new_title, suffix)
