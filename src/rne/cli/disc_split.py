"""Chapter-based episode detection algorithms for multi-episode disc files."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass

from rne.probe import Chapter


@dataclass
class Episode:
    number: int
    chapter_start: int   # 1-based, inclusive
    chapter_end: int     # 1-based, inclusive
    chapters: list[Chapter]

    @property
    def duration(self) -> float:
        return sum(ch.duration for ch in self.chapters)

    def duration_str(self) -> str:
        m, s = divmod(int(self.duration), 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def autodetect(chapters: list[Chapter], target_sec: float) -> list[list[Chapter]]:
    """Greedily group chapters into episodes targeting target_sec duration each.

    Accumulates chapters until the running total falls within ±35% of target.
    Short chapters (OP/ED/preview) are naturally absorbed because they never
    reach the low threshold alone. Orphan chapters at the end fold into the
    last group.
    """
    lo, hi = target_sec * 0.65, target_sec * 1.35
    groups: list[list[Chapter]] = []
    group: list[Chapter] = []
    acc = 0.0

    for ch in chapters:
        group.append(ch)
        acc += ch.duration
        if acc >= lo:
            next_idx = chapters.index(ch) + 1
            next_dur = chapters[next_idx].duration if next_idx < len(chapters) else 0.0
            if acc >= target_sec or (acc + next_dur) > hi:
                groups.append(group)
                group, acc = [], 0.0

    if group:
        if groups:
            groups[-1].extend(group)
        else:
            groups.append(group)

    return groups


def groups_to_episodes(groups: list[list[Chapter]], start_ep: int) -> list[Episode]:
    """Convert chapter groups to Episode objects with correct 1-based chapter offsets."""
    episodes, cursor = [], 1
    for i, group in enumerate(groups):
        episodes.append(
            Episode(
                number=start_ep + i,
                chapter_start=cursor,
                chapter_end=cursor + len(group) - 1,
                chapters=group,
            )
        )
        cursor += len(group)
    return episodes


def fixed_split(chapters: list[Chapter], n: int, start_ep: int) -> list[Episode]:
    """Split chapters into groups of exactly n chapters each."""
    groups = [chapters[i : i + n] for i in range(0, len(chapters), n)]
    return groups_to_episodes(groups, start_ep)


def manual_entry(chapters: list[Chapter], start_ep: int) -> list[Episode]:
    """Prompt the user to enter chapter ranges one episode at a time."""
    print(f"\n  Total chapters: {len(chapters)}")
    print("  Enter chapter ranges as START-END (e.g. 1-5). Blank line when done.\n")
    episodes: list[Episode] = []
    ep_num = start_ep
    while True:
        raw = input(f"  E{ep_num:02d} chapter range (or blank to finish): ").strip()
        if not raw:
            break
        m = re.match(r"^(\d+)-(\d+)$", raw)
        if not m:
            print("  Use format START-END (e.g. 1-5)", file=sys.stderr)
            continue
        s, e = int(m.group(1)), int(m.group(2))
        if s < 1 or e > len(chapters) or s > e:
            print(f"  Out of range — chapters are 1-{len(chapters)}.", file=sys.stderr)
            continue
        episodes.append(Episode(ep_num, s, e, chapters[s - 1 : e]))
        ep_num += 1
    return episodes
