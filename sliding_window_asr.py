"""Shared sliding-window and speaker-alignment primitives for diarized ASR."""

from __future__ import annotations

import os
from dataclasses import dataclass


DEFAULT_WINDOW_SECONDS = float(
    os.environ.get("DIARIZATION_ASR_WINDOW_SECONDS", "45")
)
DEFAULT_OVERLAP_SECONDS = float(
    os.environ.get("DIARIZATION_ASR_OVERLAP_SECONDS", "15")
)


@dataclass(frozen=True)
class SlidingWindow:
    """One ASR window and the central interval whose hypotheses it owns."""

    start: float
    end: float
    keep_start: float
    keep_end: float
    is_last: bool = False


@dataclass(frozen=True)
class TimedUnit:
    """A timestamped ASR word or subword token on the meeting timeline."""

    text: str
    start: float
    end: float

    @property
    def midpoint(self) -> float:
        return (self.start + self.end) / 2


def build_sliding_windows(
    duration: float,
    *,
    window_seconds: float = DEFAULT_WINDOW_SECONDS,
    overlap_seconds: float = DEFAULT_OVERLAP_SECONDS,
) -> list[SlidingWindow]:
    """Cover an audio timeline with overlapping windows and unique keep zones."""
    duration = float(duration)
    window_seconds = float(window_seconds)
    overlap_seconds = float(overlap_seconds)
    if duration <= 0:
        return []
    if window_seconds <= 0:
        raise ValueError("ASR window duration must be greater than zero.")
    if overlap_seconds < 0 or overlap_seconds >= window_seconds:
        raise ValueError("ASR overlap must be at least zero and shorter than the window.")

    step = window_seconds - overlap_seconds
    bounds: list[tuple[float, float]] = []
    start = 0.0
    while start < duration:
        end = min(duration, start + window_seconds)
        bounds.append((start, end))
        if end >= duration:
            break
        start += step

    windows = []
    for index, (start, end) in enumerate(bounds):
        keep_start = (
            0.0
            if index == 0
            else (bounds[index - 1][1] + start) / 2
        )
        keep_end = (
            duration
            if index == len(bounds) - 1
            else (end + bounds[index + 1][0]) / 2
        )
        windows.append(
            SlidingWindow(
                start=start,
                end=end,
                keep_start=keep_start,
                keep_end=keep_end,
                is_last=index == len(bounds) - 1,
            )
        )
    return windows


def window_owns(unit: TimedUnit, window: SlidingWindow) -> bool:
    """Return whether this window owns the unit inside the overlap region."""
    timestamp = unit.midpoint
    if window.is_last:
        return window.keep_start <= timestamp <= window.keep_end
    return window.keep_start <= timestamp < window.keep_end


def _overlap(start_a: float, end_a: float, start_b: float, end_b: float) -> float:
    return max(0.0, min(end_a, end_b) - max(start_a, start_b))


def _distance_to_turn(timestamp: float, turn) -> float:
    if turn.start <= timestamp <= turn.end:
        return 0.0
    return min(abs(timestamp - turn.start), abs(timestamp - turn.end))


def assign_units_to_turns(units: list[TimedUnit], turns: list) -> list[list[TimedUnit]]:
    """Assign every ASR unit to the best overlapping, then nearest, speaker turn."""
    assignments: list[list[TimedUnit]] = [[] for _ in turns]
    if not turns:
        return assignments

    for unit in sorted(units, key=lambda item: (item.start, item.end)):
        midpoint = unit.midpoint
        scores = []
        for index, turn in enumerate(turns):
            overlap = _overlap(unit.start, unit.end, turn.start, turn.end)
            distance = _distance_to_turn(midpoint, turn)
            scores.append((overlap, -distance, -index, index))
        best_index = max(scores)[3]
        assignments[best_index].append(unit)
    return assignments

