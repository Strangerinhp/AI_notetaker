"""
summarize.py
------------
Gọi Ollama (chạy local trên máy, mặc định cổng 11434) để tóm tắt transcript
thành biên bản họp có cấu trúc. Không có dữ liệu nào được gửi ra ngoài máy.
"""

import os

import requests

OLLAMA_BASE_URL = "http://localhost:11434"
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_MODEL = "gemini-3.6-flash"
DEFAULT_MODEL = "gemma4:e2b"  # đổi thành model bạn đã "ollama pull" sẵn

MEETING_MINUTES_PROMPT = """\
Bạn là trợ lý ghi biên bản cuộc họp. Dựa vào bản ghi âm (transcript) dưới đây, \
hãy viết biên bản họp chuyên nghiệp, súc tích, gồm các phần:

1. Tóm tắt chung (2-3 câu)
2. Các nội dung chính đã thảo luận (gạch đầu dòng)
3. Quyết định đã chốt (nếu có)
4. Việc cần làm tiếp theo / Action items (ai làm gì, nếu transcript có đề cập)

Transcript:
---
{transcript}
---

Chỉ trả về biên bản họp, không thêm lời dẫn hay giải thích khác.
"""


def query_ollama(prompt: str, model: str = DEFAULT_MODEL, timeout: int = 600) -> str:
    """
    Gửi 1 prompt tới Ollama đang chạy local và lấy kết quả.

    Args:
        prompt: nội dung prompt.
        model: tên model đã pull trong Ollama (vd "gemma3:27b", "phi4", "llama3.1").
        timeout: thời gian chờ tối đa (giây) — model lớn/transcript dài cần lâu hơn.

    Returns:
        Văn bản phản hồi từ model.
    """
    url = f"{OLLAMA_BASE_URL}/api/generate"
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
    }

    try:
        response = requests.post(url, json=payload, timeout=timeout)
        response.raise_for_status()
    except requests.exceptions.ConnectionError as e:
        raise RuntimeError(
            "Không kết nối được tới Ollama. Hãy chắc chắn Ollama đang chạy "
            "('ollama serve') và model đã được pull ('ollama pull <model>')."
        ) from e
    except requests.exceptions.Timeout as e:
        raise RuntimeError(
            "Ollama phản hồi quá lâu (timeout). Transcript có thể quá dài, "
            "hãy thử model nhỏ hơn hoặc tăng timeout."
        ) from e

    data = response.json()
    return data.get("response", "").strip()


def query_gemini(prompt: str, model: str = GEMINI_MODEL, timeout: int = 600) -> str:
    """Send a prompt to the Gemini API and return its text response."""
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError(
            "Gemini API key not found. Set GEMINI_API_KEY or GOOGLE_API_KEY."
        )

    url = GEMINI_API_URL.format(model=model)
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}],
            }
        ]
    }

    try:
        response = requests.post(
            url,
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": api_key,
            },
            json=payload,
            timeout=timeout,
        )
        response.raise_for_status()
    except requests.exceptions.ConnectionError as e:
        raise RuntimeError("Could not connect to the Gemini API.") from e
    except requests.exceptions.Timeout as e:
        raise RuntimeError("The Gemini API request timed out.") from e
    except requests.exceptions.HTTPError as e:
        try:
            error_message = response.json().get("error", {}).get("message")
        except (ValueError, AttributeError):
            error_message = None
        detail = error_message or response.text[:300] or "Unknown API error"
        raise RuntimeError(
            f"Gemini API returned HTTP {response.status_code}: {detail}"
        ) from e

    data = response.json()
    candidates = data.get("candidates") or []
    if not candidates:
        block_reason = data.get("promptFeedback", {}).get("blockReason")
        detail = f" Prompt blocked: {block_reason}." if block_reason else ""
        raise RuntimeError(f"Gemini API returned no response.{detail}")

    parts = candidates[0].get("content", {}).get("parts", [])
    text = "".join(part.get("text", "") for part in parts).strip()
    if not text:
        finish_reason = candidates[0].get("finishReason", "unknown")
        raise RuntimeError(
            f"Gemini API returned an empty response (finish reason: {finish_reason})."
        )
    return text


def summarize_transcript(
    transcript: str,
    model: str = DEFAULT_MODEL,
    use_gemini_api: bool = False,
) -> str:
    """Tạo biên bản họp từ transcript đầy đủ."""
    if not transcript.strip():
        return "(Không có nội dung transcript để tóm tắt.)"

    prompt = MEETING_MINUTES_PROMPT.format(transcript=transcript)
    if use_gemini_api:
        return query_gemini(prompt)
    return query_ollama(prompt, model=model)
