"""
transcribe_whisper.py
---------------------
Dùng model Whisper (OpenAI, chạy local) để chuyển audio thành văn bản.
Model chỉ được load 1 lần và tái sử dụng cho mọi request.

Lưu ý:
- Cần set PYTORCH_ENABLE_MPS_FALLBACK=1 TRƯỚC khi import torch
  (một số toán tử Whisper chưa hỗ trợ đầy đủ trên MPS)
"""

import os
import ssl
import threading

# TODO: Tắt SSL verify (cần thiết trên một số máy khi download model Whisper)
ssl._create_default_https_context = ssl._create_unverified_context

# TODO: Set biến môi trường cho MPS fallback (PHẢI đặt trước import torch)
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

import torch
import whisper
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

# ============================================================
# CẤU HÌNH
# ============================================================
# Các model Whisper: "tiny", "base", "small", "medium", "large-v3"
# Model lớn hơn → chính xác hơn nhưng chậm hơn, tốn RAM hơn
DEFAULT_MODEL_SIZE = "large-v3"


# ============================================================
# HÀM CHỌN DEVICE
# ============================================================

def get_device() -> str:
    """Tự động chọn thiết bị tốt nhất hiện có: MPS (Apple Silicon) > CUDA > CPU."""
    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


DEFAULT_DEVICE = get_device()


# ============================================================
# LOAD MODEL (LAZY LOAD - chỉ load 1 lần)
# ============================================================
_model = None
_inference_lock = threading.Lock()


def get_model(model_size: str = DEFAULT_MODEL_SIZE, device: str = None):
    """Load model Whisper (lazy load, chỉ load 1 lần)."""
    global _model
    if _model is None:
        device = device or DEFAULT_DEVICE
        print(f"[transcribe] Đang load Whisper model '{model_size}' trên device '{device}'...")
        _model = whisper.load_model(model_size, device=device)
        print("[transcribe] Model đã sẵn sàng.")
    return _model



# ============================================================
# TRANSCRIBE 1 ĐOẠN AUDIO
# ============================================================

def transcribe_segment(segment_path: str, language: str = None) -> str:
    """
    Chuyển 1 đoạn audio thành văn bản.

    Args:
        segment_path: đường dẫn file audio đoạn nhỏ.
        language: mã ngôn ngữ (vd "vi", "en"). None -> Whisper tự nhận diện.

    Returns:
        Văn bản đã transcribe (đã strip khoảng trắng thừa).
    """
    model = get_model()
    # fp16 chỉ được hỗ trợ ổn định trên CUDA; trên MPS/CPU dùng fp32 để tránh lỗi/NaN.
    use_fp16 = model.device.type == "cuda"
    with _inference_lock:
        result = model.transcribe(segment_path, language=language, fp16=use_fp16)
    return result.get("text", "").strip()


# ============================================================
# TRANSCRIBE NHIỀU ĐOẠN VÀ GHÉP LẠI
# ============================================================

def transcribe_segments(segment_paths: list[str], language: str = None,
                         progress_callback=None) -> str:
    """
    Transcribe nhiều đoạn audio liên tiếp và ghép lại thành 1 văn bản đầy đủ.

    Args:
        segment_paths: danh sách đường dẫn các đoạn audio (đúng thứ tự).
        language: mã ngôn ngữ, None để tự nhận diện.
        progress_callback: hàm callback(current_index, total) để báo tiến độ.

    Returns:
        Văn bản transcript đầy đủ, các đoạn nối bằng dấu xuống dòng.
    """
    full_text_parts = []
    total = len(segment_paths)

    for idx, path in enumerate(segment_paths, start=1):
        text = transcribe_segment(path, language=language)
        full_text_parts.append(text)
        if progress_callback:
            progress_callback(idx, total)

    return "\n".join(full_text_parts)


def transcribe_diarized_segments(
    turns: list[SpeakerTurn],
    language: str = None,
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
    language: str = None,
    progress_callback=None,
    *,
    window_seconds: float = DEFAULT_WINDOW_SECONDS,
    overlap_seconds: float = DEFAULT_OVERLAP_SECONDS,
) -> str:
    """Run Whisper on overlapping windows, then align timed words to speakers."""
    samples, sample_rate = sf.read(audio_path, dtype="float32", always_2d=False)
    if samples.ndim > 1:
        samples = samples.mean(axis=1)
    if sample_rate != whisper.audio.SAMPLE_RATE:
        raise RuntimeError(
            f"Whisper expects {whisper.audio.SAMPLE_RATE} Hz audio, "
            f"received {sample_rate} Hz."
        )

    duration = samples.size / sample_rate
    windows = build_sliding_windows(
        duration,
        window_seconds=window_seconds,
        overlap_seconds=overlap_seconds,
    )
    model = get_model()
    use_fp16 = model.device.type == "cuda"
    timed_units: list[TimedUnit] = []

    for index, window in enumerate(windows, start=1):
        first_sample = int(round(window.start * sample_rate))
        final_sample = int(round(window.end * sample_rate))
        window_audio = samples[first_sample:final_sample]
        with _inference_lock:
            result = model.transcribe(
                window_audio,
                language=language,
                fp16=use_fp16,
                word_timestamps=True,
                condition_on_previous_text=False,
                verbose=False,
            )

        for segment in result.get("segments", []):
            words = segment.get("words") or []
            if words:
                candidates = [
                    (
                        str(word.get("word", "")),
                        float(word.get("start", segment.get("start", 0.0))),
                        float(word.get("end", segment.get("end", 0.0))),
                    )
                    for word in words
                ]
            else:
                candidates = [(
                    str(segment.get("text", "")),
                    float(segment.get("start", 0.0)),
                    float(segment.get("end", 0.0)),
                )]

            for text, local_start, local_end in candidates:
                if not text.strip():
                    continue
                unit = TimedUnit(
                    text=text,
                    start=window.start + max(0.0, local_start),
                    end=window.start + max(local_start, local_end),
                )
                if window_owns(unit, window):
                    timed_units.append(unit)

        if progress_callback:
            progress_callback(index, len(windows))

    assignments = assign_units_to_turns(timed_units, turns)
    lines = []
    for turn, units in zip(turns, assignments):
        text = " ".join("".join(unit.text for unit in units).split())
        if text:
            lines.append(format_speaker_turn(turn, text))
    return "\n".join(lines)
