"""Vietnamese transcription with hynt Zipformer and sherpa-onnx.

The model is distributed under CC BY-NC-ND 4.0.  It is suitable for this
project's research/demo workflow, but it must not silently be presented as a
commercially licensed model.
"""

from __future__ import annotations

import os
import re
import threading

import numpy as np
import sherpa_onnx
import soundfile as sf
from huggingface_hub import hf_hub_download
from huggingface_hub.errors import LocalEntryNotFoundError

from speaker_diarization import SpeakerTurn, format_speaker_turn
from sliding_window_asr import (
    DEFAULT_OVERLAP_SECONDS,
    DEFAULT_WINDOW_SECONDS,
    TimedUnit,
    assign_units_to_turns,
    build_sliding_windows,
    window_owns,
)


REPO_ID = "hynt/Zipformer-30M-RNNT-6000h"
SAMPLE_RATE = 16_000
FEATURE_DIM = 80
TIMESTAMP_INTERVAL_SECONDS = 30

ONNX_FILES = {
    "fp32": {
        "encoder": "encoder-epoch-20-avg-10.onnx",
        "decoder": "decoder-epoch-20-avg-10.onnx",
        "joiner": "joiner-epoch-20-avg-10.onnx",
    },
    "int8": {
        "encoder": "encoder-epoch-20-avg-10.int8.onnx",
        "decoder": "decoder-epoch-20-avg-10.int8.onnx",
        "joiner": "joiner-epoch-20-avg-10.int8.onnx",
    },
}

# Despite its name, config.json is the sherpa token table in this model repo.
TOKEN_FILE = "config.json"
QUANTIZATION = os.environ.get("ZIPFORMER_QUANTIZATION", "int8").strip().lower()
DECODING_METHOD = os.environ.get(
    "ZIPFORMER_DECODING_METHOD",
    "modified_beam_search",
).strip()
NUM_THREADS = int(os.environ.get("ZIPFORMER_NUM_THREADS", "4"))

_recognizer = None
_model_lock = threading.Lock()
_inference_lock = threading.Lock()


def _cached_or_download(filename: str) -> str:
    """Use a cached model file, downloading it only when it is absent."""
    try:
        return hf_hub_download(
            repo_id=REPO_ID,
            filename=filename,
            local_files_only=True,
        )
    except LocalEntryNotFoundError:
        return hf_hub_download(repo_id=REPO_ID, filename=filename)


def download_model() -> dict[str, str]:
    """Resolve the selected Zipformer ONNX files and token table."""
    if QUANTIZATION not in ONNX_FILES:
        supported = ", ".join(sorted(ONNX_FILES))
        raise RuntimeError(
            f"ZIPFORMER_QUANTIZATION must be one of: {supported}."
        )
    paths = {
        key: _cached_or_download(filename)
        for key, filename in ONNX_FILES[QUANTIZATION].items()
    }
    paths["tokens"] = _cached_or_download(TOKEN_FILE)
    return paths


def create_recognizer(model_paths: dict[str, str]):
    """Create the offline transducer recognizer used by this model."""
    return sherpa_onnx.OfflineRecognizer.from_transducer(
        encoder=model_paths["encoder"],
        decoder=model_paths["decoder"],
        joiner=model_paths["joiner"],
        tokens=model_paths["tokens"],
        num_threads=NUM_THREADS,
        sample_rate=SAMPLE_RATE,
        feature_dim=FEATURE_DIM,
        decoding_method=DECODING_METHOD,
    )


def get_recognizer():
    """Load the model once on first use."""
    global _recognizer
    if _recognizer is None:
        with _model_lock:
            if _recognizer is None:
                print(f"[zipformer] Loading {QUANTIZATION} ONNX model...")
                _recognizer = create_recognizer(download_model())
                print("[zipformer] Model is ready.")
    return _recognizer


def _sentence_case(text: str) -> str:
    normalized = " ".join(str(text).split()).strip()
    if not normalized:
        return ""
    return normalized[0].upper() + normalized[1:]


def _format_timestamp(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _clean_token_text(tokens: list[str]) -> str:
    text = "".join(tokens).replace("▁", " ")
    text = re.sub(r"\s+([,.;:!?%])", r"\1", text)
    return _sentence_case(text)


def _result_timestamp_buckets(
    result,
    segment_offset: float,
) -> dict[int, list[str]]:
    """Group recognized tokens into global 30-second timeline buckets."""
    buckets: dict[int, list[str]] = {}
    for token, timestamp in zip(
        getattr(result, "tokens", []),
        getattr(result, "timestamps", []),
    ):
        if not str(token).strip():
            continue
        global_seconds = segment_offset + max(0.0, float(timestamp))
        bucket = (
            int(global_seconds // TIMESTAMP_INTERVAL_SECONDS)
            * TIMESTAMP_INTERVAL_SECONDS
        )
        buckets.setdefault(bucket, []).append(str(token))
    return buckets


def _recognize_samples(samples: np.ndarray):
    recognizer = get_recognizer()
    with _inference_lock:
        stream = recognizer.create_stream()
        stream.accept_waveform(SAMPLE_RATE, np.asarray(samples, dtype=np.float32))
        recognizer.decode_streams([stream])
        return stream.result


def _transcribe_segment_result(segment_path: str, language: str | None = None):
    del language  # This checkpoint is Vietnamese-only.
    samples, sample_rate = sf.read(
        segment_path,
        dtype="float32",
        always_2d=False,
    )
    if samples.ndim > 1:
        samples = samples.mean(axis=1)
    if sample_rate != SAMPLE_RATE:
        raise RuntimeError(
            f"Zipformer expects {SAMPLE_RATE} Hz audio, received {sample_rate} Hz."
        )
    return _recognize_samples(samples), samples.size / SAMPLE_RATE


def transcribe_segment(segment_path: str, language: str | None = None) -> str:
    result, _duration = _transcribe_segment_result(segment_path, language=language)
    return _sentence_case(getattr(result, "text", ""))


def transcribe_segments(
    segment_paths: list[str],
    language: str | None = None,
    progress_callback=None,
) -> str:
    """Transcribe continuous WAV chunks with 30-second timeline markers."""
    timeline_buckets: dict[int, list[str]] = {}
    plain_text_parts = []
    timestamps_available = True
    segment_offset = 0.0
    total = len(segment_paths)

    for index, path in enumerate(segment_paths, start=1):
        result, duration = _transcribe_segment_result(path, language=language)
        segment_buckets = _result_timestamp_buckets(result, segment_offset)
        plain_text = _sentence_case(getattr(result, "text", ""))
        if plain_text:
            plain_text_parts.append(plain_text)
            if not segment_buckets:
                timestamps_available = False
        for bucket, tokens in segment_buckets.items():
            timeline_buckets.setdefault(bucket, []).extend(tokens)
        segment_offset += duration
        if progress_callback:
            progress_callback(index, total)

    if not timestamps_available:
        return "\n".join(plain_text_parts)

    lines = []
    for bucket in sorted(timeline_buckets):
        text = _clean_token_text(timeline_buckets[bucket])
        if text:
            lines.append(f"[{_format_timestamp(bucket)}] {text}")
    return "\n".join(lines)


def transcribe_diarized_audio(
    audio_path: str,
    turns: list[SpeakerTurn],
    language: str | None = None,
    progress_callback=None,
    *,
    window_seconds: float = DEFAULT_WINDOW_SECONDS,
    overlap_seconds: float = DEFAULT_OVERLAP_SECONDS,
) -> str:
    """Run overlapping Zipformer windows and align timed tokens to speakers."""
    del language
    samples, sample_rate = sf.read(audio_path, dtype="float32", always_2d=False)
    if samples.ndim > 1:
        samples = samples.mean(axis=1)
    if sample_rate != SAMPLE_RATE:
        raise RuntimeError(
            f"Zipformer expects {SAMPLE_RATE} Hz audio, received {sample_rate} Hz."
        )

    windows = build_sliding_windows(
        samples.size / SAMPLE_RATE,
        window_seconds=window_seconds,
        overlap_seconds=overlap_seconds,
    )
    timed_units: list[TimedUnit] = []

    for index, window in enumerate(windows, start=1):
        first_sample = int(round(window.start * SAMPLE_RATE))
        final_sample = int(round(window.end * SAMPLE_RATE))
        result = _recognize_samples(samples[first_sample:final_sample])
        tokens = list(getattr(result, "tokens", []) or [])
        timestamps = list(getattr(result, "timestamps", []) or [])

        if tokens and len(tokens) == len(timestamps):
            for token, local_timestamp in zip(tokens, timestamps):
                if not str(token).strip():
                    continue
                start = window.start + max(0.0, float(local_timestamp))
                unit = TimedUnit(str(token), start, start)
                if window_owns(unit, window):
                    timed_units.append(unit)
        else:
            # Compatibility fallback for sherpa builds that omit token times.
            text = _sentence_case(getattr(result, "text", ""))
            if text:
                fallback_tokens = [f"▁{word}" for word in text.split()]
                span = max(window.keep_end - window.keep_start, 0.001)
                for token_index, token in enumerate(fallback_tokens):
                    start = window.keep_start + span * token_index / len(fallback_tokens)
                    end = window.keep_start + span * (token_index + 1) / len(fallback_tokens)
                    timed_units.append(TimedUnit(token, start, end))

        if progress_callback:
            progress_callback(index, len(windows))

    assignments = assign_units_to_turns(timed_units, turns)
    lines = []
    for turn, units in zip(turns, assignments):
        text = _clean_token_text([unit.text for unit in units])
        if text:
            lines.append(format_speaker_turn(turn, text))
    return "\n".join(lines)
