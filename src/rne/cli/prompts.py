from __future__ import annotations

import sys

from rne import config
from rne.makemkv import parse_index_spec
from rne.models import AudioTrack
from rne.probe import AudioStream


def prompt_with_default(question: str, default: str) -> str:
    """Print 'question [default]: ' and return stripped input, or default on empty."""
    answer = input(f"{question} [{default}]: ").strip()
    return answer if answer else default


def prompt_index_spec(question: str, valid_indexes: list[int]) -> list[int] | None:
    """Prompt for an index spec; returns resolved sorted list, or None on empty input.

    Loops on invalid input. Returns the full valid_indexes list when user enters 'all'.
    """
    while True:
        raw = input(question).strip()
        if not raw:
            return None
        result = parse_index_spec(raw)
        if result is None:
            return list(valid_indexes)
        invalid = [i for i in result if i not in valid_indexes]
        if invalid:
            print(
                f"Invalid indexes: {invalid}. Valid: {valid_indexes}", file=sys.stderr
            )
            continue
        return result


def prompt_yes_no(question: str, default: bool = True) -> bool:
    """Prompt with Y/n or y/N hint; empty input returns default."""
    hint = "[Y/n]" if default else "[y/N]"
    while True:
        raw = input(f"{question} {hint}: ").strip().lower()
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        print("Please enter y or n.", file=sys.stderr)


def confirm_or_edit(preview_text: str) -> str:
    """Print preview_text then prompt [Y/n/edit]. Returns 'yes', 'no', or 'edit'."""
    print(preview_text)
    while True:
        raw = input("[Y/n/edit]: ").strip().lower()
        if not raw or raw in ("y", "yes"):
            return "yes"
        if raw in ("n", "no"):
            return "no"
        if raw in ("e", "edit"):
            return "edit"
        print("Please enter y, n, or edit.", file=sys.stderr)


def prompt_audio_track_decision(stream: AudioStream, track_num: int) -> AudioTrack:
    """Decide copy or transcode for a single audio stream.

    Returns immediately (no prompt) for copy-friendly codecs.
    Prompts for Y/n/c otherwise, with AC3 transcode as the recommended default.
    """
    if stream.codec.lower() in config.COPY_FRIENDLY_AUDIO_CODECS:
        return AudioTrack(track=track_num, codec="copy")

    channels = stream.channels if stream.channels is not None else 2
    recommended_bitrate = config.AC3_BITRATE_BY_CHANNELS.get(channels, 640)
    ch_str = f"{channels}ch"

    print(f"Track {track_num} is {stream.codec} {ch_str}. Transcode? [Y/n/c]")
    print(f"  Y - transcode to AC3 {ch_str} @ {recommended_bitrate}k (recommended)")
    print("  n - copy as-is (lossless, large file)")
    print("  c - custom codec/bitrate")

    while True:
        raw = input("> ").strip().lower()
        if not raw or raw == "y":
            return AudioTrack(track=track_num, codec="ac3", bitrate=recommended_bitrate)
        if raw == "n":
            return AudioTrack(track=track_num, codec="copy")
        if raw == "c":
            codec = input("Codec name: ").strip()
            while True:
                br_str = input("Bitrate (kbps): ").strip()
                try:
                    br = int(br_str)
                    if br > 0:
                        break
                except ValueError:
                    pass
                print("Please enter a positive integer.", file=sys.stderr)
            return AudioTrack(track=track_num, codec=codec, bitrate=br)
        print("Please enter y, n, or c.", file=sys.stderr)
