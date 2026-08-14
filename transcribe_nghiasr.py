"""Local Vietnamese-English transcription with NghiASR and sherpa-onnx."""

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

REPO_ID = "NghiMe/NghiASR"
SAMPLE_RATE = 16000
FEATURE_DIM = 80

ONNX_FILES = {
    "fp32": {
        "encoder": "encoder-epoch-4-avg-4.onnx",
        "decoder": "decoder-epoch-4-avg-4.onnx",
        "joiner": "joiner-epoch-4-avg-4.onnx",
    },
    "int8": {
        "encoder": "encoder-epoch-4-avg-4.int8.onnx",
        "decoder": "decoder-epoch-4-avg-4.int8.onnx",
        "joiner": "joiner-epoch-4-avg-4.int8.onnx",
    },
}

QUANTIZATION = "int8"
DECODING_METHOD = "modified_beam_search"
NUM_THREADS = 4
TIMESTAMP_INTERVAL_SECONDS = 30

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
    """Resolve all NghiASR model files."""
    paths = {
        key: _cached_or_download(filename)
        for key, filename in ONNX_FILES[QUANTIZATION].items()
    }
    paths["tokens"] = _cached_or_download("tokens.txt")
    return paths


def create_recognizer(model_paths: dict[str, str]):
    """Create the NghiASR offline transducer recognizer."""
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
    """Load the NghiASR model once, on first use."""
    global _recognizer
    if _recognizer is None:
        with _model_lock:
            if _recognizer is None:
                print(f"[nghiasr] Loading {QUANTIZATION} ONNX model...")
                _recognizer = create_recognizer(download_model())
                print("[nghiasr] Model is ready.")
    return _recognizer


def _sentence_case(text: str) -> str:
    normalized = " ".join(text.split()).strip()
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
    timed_tokens = [
        (token, float(timestamp))
        for token, timestamp in zip(result.tokens, result.timestamps)
        if token.strip()
    ]

    for token, local_seconds in timed_tokens:
        global_seconds = segment_offset + max(0.0, local_seconds)
        bucket = (
            int(global_seconds // TIMESTAMP_INTERVAL_SECONDS)
            * TIMESTAMP_INTERVAL_SECONDS
        )
        buckets.setdefault(bucket, []).append(token)
    return buckets


def _transcribe_segment_result(segment_path: str, language: str | None = None):
    """Return one segment's recognizer result and exact audio duration."""
    del language  # NghiASR handles Vietnamese and English without a language hint.

    samples, sample_rate = sf.read(segment_path, dtype="float32")
    if samples.ndim > 1:
        samples = samples.mean(axis=1)
    if sample_rate != SAMPLE_RATE:
        raise RuntimeError(
            f"NghiASR expects {SAMPLE_RATE} Hz audio, received {sample_rate} Hz."
        )

    return _recognize_samples(samples), samples.size / SAMPLE_RATE


def _recognize_samples(samples: np.ndarray):
    """Decode one in-memory 16 kHz mono waveform with the shared recognizer."""
    recognizer = get_recognizer()
    with _inference_lock:
        stream = recognizer.create_stream()
        stream.accept_waveform(SAMPLE_RATE, np.asarray(samples, dtype=np.float32))
        recognizer.decode_streams([stream])
        return stream.result


def transcribe_segment(segment_path: str, language: str | None = None) -> str:
    """Transcribe one 16 kHz WAV segment without timeline formatting."""
    result, _ = _transcribe_segment_result(segment_path, language=language)
    return _sentence_case(result.text)


def transcribe_segments(
    segment_paths: list[str],
    language: str | None = None,
    progress_callback=None,
) -> str:
    """Transcribe WAV segments and render real 30-second timeline markers."""
    timeline_buckets: dict[int, list[str]] = {}
    plain_text_parts = []
    timestamps_available = True
    segment_offset = 0.0
    total = len(segment_paths)

    for index, path in enumerate(segment_paths, start=1):
        result, duration = _transcribe_segment_result(path, language=language)
        segment_buckets = _result_timestamp_buckets(
            result,
            segment_offset=segment_offset,
        )
        plain_text = _sentence_case(result.text)
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


def transcribe_diarized_segments(
    turns: list[SpeakerTurn],
    language: str | None = None,
    progress_callback=None,
) -> str:
    """Transcribe pyannote turns and retain speaker/time attribution."""
    lines = []
    total = len(turns)
    for index, turn in enumerate(turns, start=1):
        text = transcribe_segment(turn.path, language=language)
        if text:
            lines.append(format_speaker_turn(turn, text))
        if progress_callback:
            progress_callback(index, total)
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
    """Run NghiASR on overlapping windows, then align timed tokens to speakers."""
    del language  # NghiASR handles Vietnamese and English without a language hint.
    samples, sample_rate = sf.read(audio_path, dtype="float32", always_2d=False)
    if samples.ndim > 1:
        samples = samples.mean(axis=1)
    if sample_rate != SAMPLE_RATE:
        raise RuntimeError(
            f"NghiASR expects {SAMPLE_RATE} Hz audio, received {sample_rate} Hz."
        )

    duration = samples.size / SAMPLE_RATE
    windows = build_sliding_windows(
        duration,
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
                # Sherpa exposes token onsets, not reliable token end times.
                # Keep these as point events so overlap ownership and speaker
                # assignment use the timestamp emitted by the recognizer.
                unit = TimedUnit(str(token), start, start)
                if window_owns(unit, window):
                    timed_units.append(unit)
        else:
            # Timestamp-less fallback keeps the recognized text once in the
            # window's owned region. Current NghiASR exports normally provide
            # token timestamps, so this branch is only a compatibility guard.
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
