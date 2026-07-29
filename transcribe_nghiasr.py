"""Local Vietnamese-English transcription with NghiASR and sherpa-onnx."""

import re
import threading

import numpy as np
import sherpa_onnx
import soundfile as sf
from huggingface_hub import hf_hub_download
from huggingface_hub.errors import LocalEntryNotFoundError

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

    recognizer = get_recognizer()
    with _inference_lock:
        stream = recognizer.create_stream()
        stream.accept_waveform(SAMPLE_RATE, np.asarray(samples, dtype=np.float32))
        recognizer.decode_streams([stream])
        return stream.result, samples.size / SAMPLE_RATE


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
