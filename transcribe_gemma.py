"""
transcribe_gemma.py
-------------------
Dùng Gemma 4 E2B qua Ollama để transcribe audio thành văn bản.
Gemma 4 E2B hỗ trợ audio input native — gửi file audio dưới dạng base64
qua trường "images" trong Ollama API (cùng cơ chế multimodal với ảnh).

Ưu điểm:
  - Không cần cài Whisper / PyTorch
  - Dùng chung model với bước summarize
  - Nhẹ hơn, phù hợp máy không có GPU mạnh

Hạn chế:
  - Độ chính xác có thể kém hơn Whisper với audio dài/nhiều giọng
  - Phụ thuộc vào Ollama đang chạy
"""

import base64
import requests

OLLAMA_BASE_URL = "http://localhost:11434"
DEFAULT_MODEL = "gemma4:e2b"

TRANSCRIBE_PROMPT = """\
Hãy nghe đoạn audio này và chuyển toàn bộ nội dung giọng nói thành văn bản tiếng Việt. \
Chỉ trả về nội dung transcript, không thêm bất kỳ lời dẫn, giải thích hay bình luận nào khác. \
Nếu audio có nhiều người nói, hãy ghi lại tất cả lời thoại theo thứ tự thời gian."""


def transcribe_segment(segment_path: str, language: str = None,
                       model: str = DEFAULT_MODEL, timeout: int = 300) -> str:
    """
    Chuyển 1 đoạn audio thành văn bản bằng Gemma 4 E2B qua Ollama.

    Args:
        segment_path: đường dẫn file audio (wav, mp3...).
        language: mã ngôn ngữ (hiện tại dùng trong prompt, Gemma tự nhận diện).
        model: tên model Ollama (mặc định gemma4:e2b).
        timeout: thời gian chờ tối đa (giây).

    Returns:
        Văn bản đã transcribe.
    """
    # Đọc file audio và encode base64
    with open(segment_path, "rb") as f:
        audio_bytes = f.read()
    audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")

    # Xây dựng prompt theo ngôn ngữ
    if language and language != "vi":
        prompt = (
            f"Listen to this audio and transcribe all spoken content into text "
            f"(language: {language}). Only return the transcript, no explanations."
        )
    else:
        prompt = TRANSCRIBE_PROMPT

    # Gọi Ollama chat API với audio qua trường "images"
    # (Ollama sử dụng cùng field "images" cho cả image và audio multimodal)
    url = f"{OLLAMA_BASE_URL}/api/chat"
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": prompt,
                "images": [audio_b64],
            }
        ],
        "stream": False,
        "think": False,
    }

    try:
        response = requests.post(url, json=payload, timeout=timeout)
        response.raise_for_status()
    except requests.exceptions.ConnectionError as e:
        raise RuntimeError(
            "Không kết nối được tới Ollama. Hãy chắc chắn Ollama đang chạy "
            "('ollama serve') và model gemma4:e2b đã được pull."
        ) from e
    except requests.exceptions.Timeout as e:
        raise RuntimeError(
            f"Ollama phản hồi quá lâu khi transcribe (timeout {timeout}s). "
            "Thử cắt audio thành đoạn ngắn hơn."
        ) from e

    data = response.json()
    message = data.get("message", {})
    return message.get("content", "").strip()


def transcribe_segments(segment_paths: list[str], language: str = None,
                        model: str = DEFAULT_MODEL,
                        progress_callback=None) -> str:
    """
    Transcribe nhiều đoạn audio liên tiếp bằng Gemma 4 E2B và ghép thành văn bản.

    Args:
        segment_paths: danh sách đường dẫn các đoạn audio (đúng thứ tự).
        language: mã ngôn ngữ.
        model: tên model Ollama.
        progress_callback: hàm callback(current_index, total) để báo tiến độ.

    Returns:
        Văn bản transcript đầy đủ.
    """
    full_text_parts = []
    total = len(segment_paths)

    for idx, path in enumerate(segment_paths, start=1):
        text = transcribe_segment(path, language=language, model=model)
        full_text_parts.append(text)
        if progress_callback:
            progress_callback(idx, total)

    return "\n".join(full_text_parts)
