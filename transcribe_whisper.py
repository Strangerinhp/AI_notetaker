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

# TODO: Tắt SSL verify (cần thiết trên một số máy khi download model Whisper)
ssl._create_default_https_context = ssl._create_unverified_context

# TODO: Set biến môi trường cho MPS fallback (PHẢI đặt trước import torch)
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

import torch
import whisper

# ============================================================
# CẤU HÌNH
# ============================================================
# Các model Whisper: "tiny", "base", "small", "medium", "large-v3"
# Model lớn hơn → chính xác hơn nhưng chậm hơn, tốn RAM hơn
DEFAULT_MODEL_SIZE = "medium"


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