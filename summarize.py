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
DEFAULT_MODEL = "qwen3.5:9b-q8_0"  # đổi thành model bạn đã "ollama pull" sẵn

# Avoid Ollama's small runner default silently truncating long transcripts and
# the meeting-minutes instructions. Override these through the environment when
# an especially long meeting needs a larger window.
OLLAMA_NUM_CTX = int(os.environ.get("OLLAMA_NUM_CTX", "40000"))
OLLAMA_NUM_PREDICT = int(os.environ.get("OLLAMA_NUM_PREDICT", "9000"))
OLLAMA_TEMPERATURE = float(os.environ.get("OLLAMA_TEMPERATURE", "0.2"))

MEETING_MINUTES_PROMPT = """\
Bạn là chuyên viên văn phòng chịu trách nhiệm soạn thông báo kết luận cuộc họp \
bằng tiếng Việt. Hãy chuyển transcript thành một báo cáo Markdown có bố cục \
giống văn bản hành chính, nhưng nội dung phải thích nghi với chính cuộc họp này.

NGUYÊN TẮC BẮT BUỘC
- Tên báo cáo do người dùng nhập là: "{meeting_title}". Giữ nguyên chính tả và \
  dùng đúng tên này ở dòng tiêu đề thứ hai; không tự viết lại hay rút gọn.
- Transcript là nguồn sự thật duy nhất. Không tự đặt tên người, chức vụ, đơn vị, \
  ngày giờ, địa điểm, số liệu, thời hạn, quyết định hoặc trạng thái hoàn thành.
- Phân biệt rõ nội dung mới được thảo luận/đề xuất với quyết định đã được chốt.
- Giữ đầy đủ mọi kết luận và nhiệm vụ khác nhau. Chỉ gộp các ý thực sự trùng lặp; \
  độ dài báo cáo phải tỷ lệ với lượng thông tin của cuộc họp, không ép về một \
  số đoạn cố định.
- Với nhiệm vụ, giữ nguyên người/đơn vị chịu trách nhiệm, đơn vị phối hợp, thời hạn \
  và điều kiện phụ thuộc nếu transcript có nêu. Nếu có nhiệm vụ nhưng không rõ \
  người phụ trách, nhóm dưới tiêu đề "Chưa xác định người phụ trách".
- Bỏ hẳn trường hoặc mục tùy chọn không có dữ liệu. Không điền nội dung của ví dụ \
  và không dùng tên/số liệu của một cuộc họp khác.
- Không tạo phần cơ quan ban hành, số công văn, nơi nhận hoặc chữ ký vì transcript \
  thường không đủ dữ liệu pháp lý cho các trường này.
- Ngoại trừ các tiêu đề Markdown và đoạn mở đầu hành chính, mọi ý nội dung từ \
  "Đánh giá chung" trở đi phải là một mục danh sách Markdown bắt đầu bằng "- ". \
  Nếu một ý có thời hạn, đơn vị phối hợp hoặc chi tiết phụ, dùng danh sách con \
  thụt vào; không viết các ý thành đoạn văn rời không có dấu gạch đầu dòng.

KHUÔN ĐẦU RA
# THÔNG BÁO
## {meeting_title}

Nếu transcript có đủ dữ liệu, viết đoạn mở đầu theo văn phong hành chính, lần lượt \
nêu ngày họp, địa điểm, người chủ trì và thành phần tham dự. Bỏ chi tiết nào không \
được nêu rõ thay vì dùng placeholder hoặc suy đoán.

## Đánh giá chung:
- [Tình hình, kết quả và số liệu quan trọng đã được phát biểu rõ ràng]
- [Vấn đề, nguyên nhân, rủi ro và định hướng nếu transcript có nêu]

Sau phần đánh giá, tạo trực tiếp một nhóm cho mỗi cá nhân/đơn vị được giao việc; \
không thêm tiêu đề trung gian "Kết luận và giao nhiệm vụ". Ví dụ về HÌNH THỨC \
(không phải nội dung để sao chép):

### Giao [cá nhân hoặc đơn vị]:
- [Nhiệm vụ cụ thể, có thể kiểm chứng từ transcript]
  - **Thời hạn:** [chỉ ghi khi có]
  - **Phối hợp:** [chỉ ghi khi có]

## Nội dung khác:
- [Chỉ tạo mục này nếu có thông tin quan trọng không thuộc đánh giá hoặc giao nhiệm vụ]

Nếu không có quyết định hay nhiệm vụ nào được chốt, không tạo nhóm "Giao ..." và \
phản ánh các đề xuất chưa chốt trong "Đánh giá chung" hoặc "Nội dung khác".

TRANSCRIPT
---
{transcript}
---

Chỉ trả về Markdown của báo cáo, không dùng khối mã và không thêm lời dẫn hay giải thích.
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
        "options": {
            "num_ctx": OLLAMA_NUM_CTX,
            "num_predict": OLLAMA_NUM_PREDICT,
            "temperature": OLLAMA_TEMPERATURE,
        },
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
    meeting_title: str = "Kết luận cuộc họp",
) -> str:
    """Tạo biên bản họp từ transcript đầy đủ."""
    if not transcript.strip():
        return "(Không có nội dung transcript để tóm tắt.)"

    normalized_title = meeting_title.strip() or "Kết luận cuộc họp"
    prompt = MEETING_MINUTES_PROMPT.format(
        transcript=transcript,
        meeting_title=normalized_title,
    )
    if use_gemini_api:
        return query_gemini(prompt)
    return query_ollama(prompt, model=model)
