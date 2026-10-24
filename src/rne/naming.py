"""Output file and folder names. Pure: builds paths, never touches the disk.

Templates (see SPEC.md "Storage layout"), where {folder} is "{title} ({year})"
when the year is known (TVDB match or typed) and just "{title}" otherwise:

- TV:    {root}/{folder}/Season {season:02d}/{show} - S{season:02d}E{episode:02d}.mkv
- Movie: {root}/{folder}/{folder}{suffix}.mkv
"""

from __future__ import annotations

import pathlib


def episode_label(season: int, episode: int) -> str:
    return f"S{season:02d}E{episode:02d}"


def folder_name(title: str, year: int | None = None) -> str:
    """'Show (2025)' with a year, 'Show' without."""
    return f"{title} ({year})" if year else title


def title_dir(root: pathlib.Path, title: str, year: int | None = None) -> pathlib.Path:
    """The per-show / per-movie folder under a staging or library root."""
    return root / folder_name(title, year)


def tv_output_path(
    show_dir: pathlib.Path, show: str, season: int, episode: int
) -> pathlib.Path:
    return show_dir / f"Season {season:02d}" / f"{show} - {episode_label(season, episode)}.mkv"


def movie_output_path(
    movie_dir: pathlib.Path, movie: str, suffix: str = ""
) -> pathlib.Path:
    """suffix tells apart several files queued under one movie name, e.g. '_t03'."""
    return movie_dir / f"{movie}{suffix}.mkv"


def library_path(
    library_dir: pathlib.Path, output_path: str | pathlib.Path, *, is_tv: bool
) -> pathlib.Path:
    """Where an encode lands in the library, mirroring its staging layout.

    library_dir is the show/movie folder in the library, e.g.
    /mnt/media/Shows/Show (2025). TV keeps the "Season NN/file" part, a movie
    just its file name.
    """
    out = pathlib.Path(output_path)
    if is_tv:
        return library_dir / out.parent.name / out.name
    return library_dir / out.name


def library_dir_of(final_path: str | pathlib.Path, *, is_tv: bool) -> pathlib.Path:
    """Inverse of library_path: the show/movie folder a final path lives in."""
    final = pathlib.Path(final_path)
    return final.parent.parent if is_tv else final.parent


def renamed_output(
    old_output: str | pathlib.Path,
    *,
    is_tv: bool,
    old_title: str,
    new_title: str,
    season: int | None = None,
    episode: int | None = None,
    old_year: int | None = None,
    new_year: int | None = None,
) -> pathlib.Path:
    """Where a job's output goes after its name, year, season or episode is edited.

    Stays under the same root. The show/movie folder is renamed only if it
    was named after the title; a custom staging folder chosen at rip time is
    kept. A movie's disambiguating suffix (e.g. '_t03') is kept.
    """
    old = pathlib.Path(old_output)
    old_folder, new_folder = folder_name(old_title, old_year), folder_name(new_title, new_year)
    if is_tv:
        if season is None or episode is None:
            raise ValueError("TV jobs need a season and an episode")
        show_dir = old.parent.parent
        if show_dir.name == old_folder:
            show_dir = show_dir.parent / new_folder
        return tv_output_path(show_dir, new_title, season, episode)
    movie_dir = old.parent
    suffix = old.stem[len(old_folder):] if old.stem.startswith(old_folder) else ""
    if movie_dir.name == old_folder:
        movie_dir = movie_dir.parent / new_folder
    return movie_output_path(movie_dir, new_folder, suffix)
