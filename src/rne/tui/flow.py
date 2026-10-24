"""What the user has decided so far in an ingest or queue session. Pure.

The screens read and write a Draft. Building the job plan uses the same
job-entry builders and suffix rules as the CLI prompts (plan.tv_job /
plan.movie_job, the disc-split builder), so names and numbering match.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field

from rne import naming as names
from rne import plan, probe
from rne.cli._pipeline import _build_disc_split_jobs, _describe_mismatch, mungefilename
from rne.cli.disc_split import Episode
from rne.models import HandbrakeArgs


@dataclass
class Naming:
    is_tv: bool = True
    title: str = ""
    season: int | None = None
    first_episode: int | None = None
    disc_split: bool = False
    year: int | None = None  # from TVDB or typed; adds "(year)" to the folder
    tvdb_id: int | None = None  # set only by picking a TVDB match

    @property
    def clean_title(self) -> str:
        return mungefilename(self.title.strip())

    @property
    def folder(self) -> str:
        """The show/movie folder name: 'Show (2025)' or 'Show'."""
        return names.folder_name(self.clean_title, self.year)

    def errors(self) -> list[str]:
        errors = []
        if not self.clean_title:
            errors.append("name can't be empty")
        if self.year is not None and not 1880 <= self.year <= 2100:
            errors.append("year must be a 4-digit year (or blank)")
        if self.is_tv:
            if self.season is None or self.season < 0:
                errors.append("season must be 0 (specials) or more")
            if self.first_episode is None or self.first_episode < 1:
                errors.append("first episode must be 1 or more")
        return errors


@dataclass
class Source:
    path: pathlib.Path
    title_idx: int | None = None  # disc title index (ingest only)


@dataclass
class Draft:
    mode: str  # "ingest" or "queue"
    naming: Naming
    staging_root: pathlib.Path
    is_dvd: bool = False
    # ingest: disc titles chosen, in rip order; episode numbers follow this
    # order even if a title is skipped after a failed rip.
    selected_titles: list[int] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)  # files to encode, in order
    staging_override: pathlib.Path | None = None  # "rip somewhere else"
    summaries: dict[pathlib.Path, probe.StreamSummary] = field(default_factory=dict)
    probe_errors: dict[pathlib.Path, str] = field(default_factory=dict)
    template: HandbrakeArgs | None = None  # settings for every job
    overrides: dict[pathlib.Path, HandbrakeArgs] = field(default_factory=dict)
    skipped: set[pathlib.Path] = field(default_factory=set)
    chapters: list[probe.Chapter] | None = None  # disc split only
    episodes: list[Episode] | None = None  # disc split only
    batch_id: int | None = None

    # -- derived ---------------------------------------------------------------

    @property
    def staging_dir(self) -> pathlib.Path:
        """The show/movie folder outputs (and ingest raws) go under."""
        return self.staging_override or self.staging_root / self.naming.folder

    @property
    def reference(self) -> Source | None:
        return self.sources[0] if self.sources else None

    @property
    def reference_summary(self) -> probe.StreamSummary | None:
        ref = self.reference
        return self.summaries.get(ref.path) if ref else None

    def episode_numbers(self) -> list[int]:
        """Episode number per source (TV, not disc split)."""
        first = self.naming.first_episode or 1
        if self.mode == "ingest" and self.selected_titles:
            return [first + self.selected_titles.index(s.title_idx) for s in self.sources]
        return [first + i for i in range(len(self.sources))]

    def episode_preview(self, count: int | None = None) -> str:
        """'S01E05, S01E06' for the planned titles; '' when not applicable.

        count: number `count` titles from the first episode (before the rip);
        None: the numbers the sources will actually get.
        """
        n = self.naming
        if not n.is_tv or n.disc_split or n.season is None or n.first_episode is None:
            return ""
        if count is None:
            numbers = self.episode_numbers()
        else:
            numbers = list(range(n.first_episode, n.first_episode + count))
        return ", ".join(f"S{n.season:02d}E{ep:02d}" for ep in numbers)

    def default_template(self) -> HandbrakeArgs:
        summary = self.reference_summary
        if summary is None:
            return HandbrakeArgs()
        return plan.default_args(summary, self.is_dvd)

    # -- the plan ----------------------------------------------------------------

    def _base_plan(self, template: HandbrakeArgs) -> list[dict]:
        n = self.naming
        title = n.clean_title
        if n.is_tv and n.disc_split:
            ref = self.reference
            assert ref is not None and self.episodes is not None
            jobs = _build_disc_split_jobs(
                self.episodes, ref.path, title, n.season, self.staging_dir, template  # type: ignore[arg-type]
            )
            for job in jobs:
                job["source_chapters"] = self.chapters
                job["year"], job["tvdb_id"] = n.year, n.tvdb_id
            return jobs
        if n.is_tv:
            return [
                plan.tv_job(show=title, season=n.season, episode=ep,  # type: ignore[arg-type]
                            show_dir=self.staging_dir, source_path=s.path, args=template,
                            year=n.year, tvdb_id=n.tvdb_id)
                for s, ep in zip(self.sources, self.episode_numbers())
            ]
        jobs = []
        for pos, s in enumerate(self.sources):
            if len(self.sources) == 1:
                suffix = ""
            elif self.mode == "ingest":
                suffix = f"_t{s.title_idx:02d}"
            else:
                suffix = f"_{pos:02d}"
            jobs.append(plan.movie_job(movie=title, movie_dir=self.staging_dir,
                                       source_path=s.path, args=template, suffix=suffix,
                                       year=n.year, tvdb_id=n.tvdb_id))
        return jobs

    def build_plan(self) -> list[dict]:
        """Every planned job, with per-job extras for review.

        Extra keys: source_summary, layout_warning, override (bool),
        skipped (bool), errors (list[str]).
        """
        template = self.template or self.default_template()
        ref_summary = self.reference_summary
        jobs = self._base_plan(template)
        for job in jobs:
            path = pathlib.Path(job["source_path"])
            override = self.overrides.get(path)
            if override is not None:
                hb: HandbrakeArgs = job["handbrake_args"]
                job["handbrake_args"] = plan.with_chapters(
                    override, hb.chapter_start, hb.chapter_end
                )
            summary = self.summaries.get(path)
            job["source_summary"] = summary
            job["override"] = override is not None
            job["skipped"] = path in self.skipped
            job["layout_warning"] = summary is None or (
                ref_summary is not None and not probe.layouts_match(ref_summary, summary)
            )
            job["errors"] = (
                plan.validate(job["handbrake_args"], summary) if summary is not None else []
            )
            if summary is None and path in self.probe_errors:
                job["errors"].append(f"could not probe: {self.probe_errors[path]}")
        outputs = [j["output_path"] for j in jobs if not j["skipped"]]
        for job in jobs:
            if not job["skipped"] and outputs.count(job["output_path"]) > 1:
                job["errors"].append(f"output {job['output_path']} is used by more than one job")
        return jobs

    def mismatch_details(self, jobs: list[dict]) -> list[str]:
        ref = self.reference_summary
        lines = []
        for job in jobs:
            summary = job.get("source_summary")
            if ref is not None and summary is not None and job["layout_warning"]:
                lines.append(_describe_mismatch(ref, summary, job["label"]))
        return lines

    def jobs_to_queue(self) -> list[dict]:
        """The plan minus skipped jobs, ready for jobs.insert_jobs."""
        return [j for j in self.build_plan() if not j["skipped"]]

    def blocking_errors(self) -> list[str]:
        return [
            f"{j['label']}: {e}" for j in self.build_plan() if not j["skipped"] for e in j["errors"]
        ]
