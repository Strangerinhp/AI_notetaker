"""Vietnamese transcription with Qualcomm PhoASR Whisper Small.

PhoASR is a Hugging Face ``WhisperForConditionalGeneration`` checkpoint, so
it is loaded through Transformers rather than the ``openai-whisper`` package.
Heavy dependencies and model weights are loaded only when this engine is used.
"""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Callable

import numpy as np
import soundfile as sf

from speaker_diarization import SpeakerTurn, format_speaker_turn
from sliding_window_asr import (
    DEFAULT_OVERLAP_SECONDS,
    DEFAULT_WINDOW_SECONDS,
    TimedUnit,
    assign_units_to_turns,
    build_sliding_windows,
    window_owns,
)


MODEL_ID = os.environ.get(
    "PHOASR_MODEL",
    "Qualcomm-AI-Research/PhoASR-whisper-small",
)
SAMPLE_RATE = 16_000
TIMESTAMP_INTERVAL_SECONDS = 30
MAX_WINDOW_SECONDS = 30.0

_pipeline = None
_model_lock = threading.Lock()
_inference_lock = threading.Lock()


class PhoASRError(RuntimeError):
    """Actionable PhoASR error safe to display in the web interface."""


def _select_device_and_dtype(torch):
    requested_device = os.environ.get("PHOASR_DEVICE", "auto").strip().lower()
    if requested_device == "auto":
        if torch.cuda.is_available():
            device_name = "cuda"
        elif (
            getattr(torch.backends, "mps", None)
            and torch.backends.mps.is_available()
        ):
            device_name = "mps"
        else:
            device_name = "cpu"
    elif requested_device in {"cpu", "cuda", "mps"}:
        device_name = requested_device
    else:
        raise PhoASRError("PHOASR_DEVICE phải là auto, cpu, cuda hoặc mps.")

    if device_name == "cuda" and not torch.cuda.is_available():
        raise PhoASRError(
            "PHOASR_DEVICE=cuda nhưng PyTorch không nhận diện GPU CUDA."
        )
    if device_name == "mps" and not (
        getattr(torch.backends, "mps", None) and torch.backends.mps.is_available()
    ):
        raise PhoASRError("PHOASR_DEVICE=mps nhưng PyTorch không nhận diện Apple MPS.")

    requested_dtype = os.environ.get("PHOASR_DTYPE", "auto").strip().lower()
    if requested_dtype == "auto":
        dtype_name = "float16" if device_name == "cuda" else "float32"
    else:
        dtype_name = requested_dtype
    dtype_map = {
        "float16": torch.float16,
        "float32": torch.float32,
        "bfloat16": torch.bfloat16,
    }
    if dtype_name not in dtype_map:
        raise PhoASRError(
            "PHOASR_DTYPE phải là auto, float16, float32 hoặc bfloat16."
        )
    if device_name == "cpu" and dtype_name == "float16":
        raise PhoASRError(
            "PhoASR không hỗ trợ ổn định float16 trên CPU; hãy dùng float32."
        )

    pipeline_device = 0 if device_name == "cuda" else device_name
    return pipeline_device, device_name, dtype_map[dtype_name], dtype_name


def get_pipeline():
    """Load the Transformers ASR pipeline once, on first PhoASR request."""
    global _pipeline
    if _pipeline is None:
        with _model_lock:
            if _pipeline is None:
                try:
                    import torch
                    from transformers import pipeline
                except ImportError as error:
                    raise PhoASRError(
                        "Thiếu Transformers cho PhoASR. Hãy chạy: "
                        "pip install -r requirements.txt"
                    ) from error

                device, device_name, torch_dtype, dtype_name = (
                    _select_device_and_dtype(torch)
                )
                print(
                    f"[phoasr] Loading '{MODEL_ID}' on {device_name} "
                    f"with {dtype_name}..."
                )
                try:
                    _pipeline = pipeline(
                        "automatic-speech-recognition",
                        model=MODEL_ID,
                        device=device,
                        torch_dtype=torch_dtype,
                    )
                except Exception as error:
                    raise PhoASRError(f"Không thể tải model PhoASR: {error}") from error
                print("[phoasr] Model is ready.")
    return _pipeline


def _read_audio(path: str) -> tuple[np.ndarray, int]:
    samples, sample_rate = sf.read(path, dtype="float32", always_2d=False)
    if samples.ndim > 1:
        samples = samples.mean(axis=1)
    if sample_rate != SAMPLE_RATE:
        raise PhoASRError(
            f"PhoASR cần audio {SAMPLE_RATE} Hz, nhưng nhận được {sample_rate} Hz."
        )
    return np.ascontiguousarray(samples, dtype=np.float32), sample_rate


def _run_pipeline(samples: np.ndarray, language: str | None) -> dict:
    recognizer = get_pipeline()
    generate_kwargs = {
        "language": language or "vi",
        "task": "transcribe",
    }
    with _inference_lock:
        result = recognizer(
            {
                "raw": np.asarray(samples, dtype=np.float32),
                "sampling_rate": SAMPLE_RATE,
            },
            return_timestamps="word",
            generate_kwargs=generate_kwargs,
        )
    if not isinstance(result, dict):
        raise PhoASRError("PhoASR trả về kết quả không hợp lệ.")
    return result


def _clean_text(parts: list[str]) -> str:
    text = " ".join(str(part).strip() for part in parts if str(part).strip())
    text = re.sub(r"\s+([,.;:!?%])", r"\1", text)
    text = re.sub(r"([([{])\s+", r"\1", text)
    return " ".join(text.split()).strip()


def _result_units(result: dict, duration: float) -> list[TimedUnit]:
    """Convert public Transformers word chunks into local timed units."""
    units = []
    for chunk in result.get("chunks") or []:
        text = str(chunk.get("text", ""))
        timestamp = chunk.get("timestamp")
        if (
            not text.strip()
            or not isinstance(timestamp, (tuple, list))
            or len(timestamp) != 2
        ):
            continue
        start_value, end_value = timestamp
        if start_value is None and end_value is None:
            continue
        start = min(duration, max(0.0, float(start_value or 0.0)))
        end = max(start, float(end_value if end_value is not None else start))
        units.append(TimedUnit(text=text, start=start, end=min(duration, end)))

    if units:
        return units

    # Compatibility guard: current PhoASR normally returns word timestamps.
    # If a future Transformers version omits them, distribute words across the
    # window so the transcript remains usable and overlap ownership stays unique.
    words = str(result.get("text", "")).split()
    if not words:
        return []
    span = max(duration, 0.001)
    return [
        TimedUnit(
            text=word,
            start=span * index / len(words),
            end=span * (index + 1) / len(words),
        )
        for index, word in enumerate(words)
    ]


def _validate_window(window_seconds: float, overlap_seconds: float) -> None:
    if float(window_seconds) > MAX_WINDOW_SECONDS:
        raise PhoASRError(
            f"PhoASR chỉ nhận tối đa {MAX_WINDOW_SECONDS:g} giây mỗi cửa sổ."
        )
    # Let the shared builder validate the remaining constraints consistently.
    build_sliding_windows(
        1.0,
        window_seconds=window_seconds,
        overlap_seconds=overlap_seconds,
    )


def _transcribe_samples(
    samples: np.ndarray,
    language: str | None,
    *,
    window_seconds: float,
    overlap_seconds: float,
    progress_callback: Callable[[int, int], None] | None = None,
) -> list[TimedUnit]:
    _validate_window(window_seconds, overlap_seconds)
    duration = samples.size / SAMPLE_RATE
    windows = build_sliding_windows(
        duration,
        window_seconds=window_seconds,
        overlap_seconds=overlap_seconds,
    )
    timed_units = []
    for index, window in enumerate(windows, start=1):
        first_sample = int(round(window.start * SAMPLE_RATE))
        final_sample = int(round(window.end * SAMPLE_RATE))
        result = _run_pipeline(samples[first_sample:final_sample], language)
        local_duration = (final_sample - first_sample) / SAMPLE_RATE
        for local_unit in _result_units(result, local_duration):
            global_unit = TimedUnit(
                text=local_unit.text,
                start=window.start + local_unit.start,
                end=window.start + local_unit.end,
            )
            if window_owns(global_unit, window):
                timed_units.append(global_unit)
        if progress_callback:
            progress_callback(index, len(windows))
    return timed_units


def _format_timestamp(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _render_timeline(units: list[TimedUnit]) -> str:
    buckets: dict[int, list[str]] = {}
    for unit in sorted(units, key=lambda item: (item.start, item.end)):
        bucket = (
            int(unit.start // TIMESTAMP_INTERVAL_SECONDS)
            * TIMESTAMP_INTERVAL_SECONDS
        )
        buckets.setdefault(bucket, []).append(unit.text)
    return "\n".join(
        f"[{_format_timestamp(bucket)}] {text}"
        for bucket in sorted(buckets)
        if (text := _clean_text(buckets[bucket]))
    )


def transcribe_segment(segment_path: str, language: str | None = None) -> str:
    samples, _sample_rate = _read_audio(segment_path)
    units = _transcribe_samples(
        samples,
        language,
        window_seconds=DEFAULT_WINDOW_SECONDS,
        overlap_seconds=DEFAULT_OVERLAP_SECONDS,
    )
    return _clean_text([unit.text for unit in units])


def transcribe_segments(
    segment_paths: list[str],
    language: str | None = None,
    progress_callback=None,
    *,
    window_seconds: float = DEFAULT_WINDOW_SECONDS,
    overlap_seconds: float = DEFAULT_OVERLAP_SECONDS,
) -> str:
    """Transcribe consecutive WAV chunks using the shared sliding-window setup."""
    _validate_window(window_seconds, overlap_seconds)
    durations = []
    window_counts = []
    for path in segment_paths:
        info = sf.info(path)
        if info.samplerate != SAMPLE_RATE:
            raise PhoASRError(
                f"PhoASR cần audio {SAMPLE_RATE} Hz, nhưng nhận được "
                f"{info.samplerate} Hz."
            )
        duration = info.frames / info.samplerate
        durations.append(duration)
        window_counts.append(
            len(
                build_sliding_windows(
                    duration,
                    window_seconds=window_seconds,
                    overlap_seconds=overlap_seconds,
                )
            )
        )

    total_windows = sum(window_counts)
    completed_windows = 0
    segment_offset = 0.0
    all_units = []
    for segment_index, (path, duration) in enumerate(zip(segment_paths, durations)):
        samples, _sample_rate = _read_audio(path)

        def on_window_progress(current, _segment_total):
            if progress_callback:
                progress_callback(completed_windows + current, total_windows)

        local_units = _transcribe_samples(
            samples,
            language,
            window_seconds=window_seconds,
            overlap_seconds=overlap_seconds,
            progress_callback=on_window_progress,
        )
        all_units.extend(
            TimedUnit(
                text=unit.text,
                start=segment_offset + unit.start,
                end=segment_offset + unit.end,
            )
            for unit in local_units
        )
        completed_windows += window_counts[segment_index]
        segment_offset += duration
    return _render_timeline(all_units)


def transcribe_diarized_audio(
    audio_path: str,
    turns: list[SpeakerTurn],
    language: str | None = None,
    progress_callback=None,
    *,
    window_seconds: float = DEFAULT_WINDOW_SECONDS,
    overlap_seconds: float = DEFAULT_OVERLAP_SECONDS,
) -> str:
    """Transcribe overlapping windows and align PhoASR words to speakers."""
    samples, _sample_rate = _read_audio(audio_path)
    units = _transcribe_samples(
        samples,
        language,
        window_seconds=window_seconds,
        overlap_seconds=overlap_seconds,
        progress_callback=progress_callback,
    )
    assignments = assign_units_to_turns(units, turns)
    lines = []
    for turn, turn_units in zip(turns, assignments):
        text = _clean_text([unit.text for unit in turn_units])
        if text:
            lines.append(format_speaker_turn(turn, text))
    return "\n".join(lines)
