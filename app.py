"""
app.py
------
Web app self-hosted để ghi biên bản họp từ file audio, chạy 100% local:
  - Whisper (transcribe.py)  -> chuyển giọng nói thành văn bản
  - Ollama  (summarize.py)   -> tóm tắt thành biên bản họp
  - FFmpeg  (audio_utils.py) -> cắt file audio dài thành từng đoạn nhỏ

Chạy:
    python app.py
Sau đó mở trình duyệt: http://localhost:5001
"""

import argparse
import io
import json
import os
import re
import shutil
import threading
import uuid
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

from audio_utils import cleanup_files, split_audio
from summarize import GEMINI_MODEL, summarize_transcript
from transcribe_whisper import transcribe_segments as transcribe_whisper
from transcribe_gemma import transcribe_segments as transcribe_gemma

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
TEMP_FOLDER = os.path.join(BASE_DIR, "temp_segments")
RESULTS_FOLDER = os.path.join(BASE_DIR, "results")

ALLOWED_EXTENSIONS = {"mp3", "wav", "m4a", "mp4", "ogg", "flac", "webm"}
SEGMENT_MINUTES = 2
NGHIASR_SEGMENT_MINUTES = 0.5
WHISPER_LANGUAGE = "vi"  # None = tự nhận diện ngôn ngữ; đặt "vi" nếu luôn là tiếng Việt
OLLAMA_MODEL = "gemma4:e2b"  # đổi theo model bạn đã pull trong Ollama

TRANSCRIBE_ENGINE_LABELS = {
    "whisper": "Whisper",
    "gemma": "Gemma 4 E2B",
    "nghiasr": "NghiASR",
}

for folder in (UPLOAD_FOLDER, TEMP_FOLDER, RESULTS_FOLDER):
    os.makedirs(folder, exist_ok=True)

app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["USE_GEMINI_API"] = False
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024  # giới hạn 2GB / file

# Lưu trạng thái các job trong bộ nhớ. Với nhu cầu production nhiều người dùng
# đồng thời, nên thay bằng Redis/SQLite thay vì dict trong RAM.
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()
results_lock = threading.Lock()


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def update_job(job_id: str, **kwargs) -> None:
    with jobs_lock:
        jobs[job_id].update(kwargs)


def strip_transcript_timestamps(transcript: str) -> str:
    """Remove display-only timeline labels before meeting summarization."""
    return re.sub(
        r"^\[\d{2,}:\d{2}:\d{2}\]\s*",
        "",
        transcript,
        flags=re.MULTILINE,
    )


def process_audio_file(
    file_path: str,
    job_id: str,
    transcribe_engine: str = "whisper",
    meeting_title: str = "Kết luận cuộc họp",
    original_filename: str = "",
) -> None:
    """Pipeline chạy nền: cắt audio -> transcribe từng đoạn -> tóm tắt."""
    segment_dir = os.path.join(TEMP_FOLDER, job_id)
    segment_paths = []

    try:
        update_job(job_id, status="splitting", message="Đang chia nhỏ file audio...")
        segment_minutes = (
            NGHIASR_SEGMENT_MINUTES
            if transcribe_engine == "nghiasr"
            else SEGMENT_MINUTES
        )
        segment_paths = split_audio(
            file_path,
            segment_dir,
            segment_minutes=segment_minutes,
        )

        total = len(segment_paths)
        engine_label = TRANSCRIBE_ENGINE_LABELS[transcribe_engine]
        update_job(job_id, status="transcribing",
                   message=f"Đang chuyển giọng nói thành văn bản bằng {engine_label} (0/{total})...")

        def on_progress(current, total_segments):
            update_job(
                job_id,
                message=f"Đang chuyển giọng nói thành văn bản bằng {engine_label} ({current}/{total_segments})...",
            )

        if transcribe_engine == "gemma":
            transcript = transcribe_gemma(
                segment_paths, language=WHISPER_LANGUAGE, progress_callback=on_progress
            )
        elif transcribe_engine == "nghiasr":
            # Import lazily so other engines do not load/download the NghiASR model.
            from transcribe_nghiasr import transcribe_segments as transcribe_nghiasr

            transcript = transcribe_nghiasr(
                segment_paths, language=WHISPER_LANGUAGE, progress_callback=on_progress
            )
        else:
            transcript = transcribe_whisper(
                segment_paths, language=WHISPER_LANGUAGE, progress_callback=on_progress
            )

        # Một số engine (đặc biệt NghiASR) có thể trả về toàn bộ chữ in hoa.
        # Chuẩn hóa một lần tại đây để transcript hiển thị và lưu dưới dạng chữ thường.
        transcript = transcript.lower()

        update_job(job_id, status="summarizing", message="Đang tóm tắt thành biên bản họp...")
        minutes = summarize_transcript(
            strip_transcript_timestamps(transcript),
            model=OLLAMA_MODEL,
            use_gemini_api=app.config["USE_GEMINI_API"],
            meeting_title=meeting_title,
        )

        # Lưu kết quả ra file để tiện tải về / xem lại sau
        with results_lock:
            _write_result(job_id, minutes, transcript)
            _write_result_metadata(
                job_id,
                title=meeting_title,
                filename=original_filename,
                engine=transcribe_engine,
            )

        update_job(
            job_id,
            status="completed",
            message="Hoàn tất!",
            transcript=transcript,
            minutes=minutes,
        )

    except Exception as e:
        update_job(job_id, status="error", message=f"Lỗi: {e}")

    finally:
        # Dọn dẹp file tạm dù thành công hay lỗi
        cleanup_files(segment_paths)
        shutil.rmtree(segment_dir, ignore_errors=True)
        try:
            os.remove(file_path)
        except OSError:
            pass


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/upload", methods=["POST"])
def upload_file():
    if "file" not in request.files:
        return jsonify({"error": "Không tìm thấy file trong request"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "Chưa chọn file"}), 400

    if not allowed_file(file.filename):
        return jsonify({"error": f"Định dạng file không được hỗ trợ. "
                                  f"Hỗ trợ: {', '.join(sorted(ALLOWED_EXTENSIONS))}"}), 400

    meeting_title = " ".join(request.form.get("title", "").split())
    if not meeting_title:
        return jsonify({"error": "Vui lòng nhập tên báo cáo"}), 400
    if len(meeting_title) > 180:
        return jsonify({"error": "Tên báo cáo không được vượt quá 180 ký tự"}), 400

    job_id = str(uuid.uuid4())
    original_filename = file.filename
    filename = secure_filename(file.filename)
    stored_name = f"{job_id}_{filename}"
    file_path = os.path.join(app.config["UPLOAD_FOLDER"], stored_name)
    file.save(file_path)

    # Lấy engine transcribe từ form data (mặc định whisper)
    transcribe_engine = request.form.get("engine", "whisper")
    if transcribe_engine not in TRANSCRIBE_ENGINE_LABELS:
        transcribe_engine = "whisper"

    with jobs_lock:
        jobs[job_id] = {
            "status": "queued",
            "message": "Đang chờ xử lý...",
            "filename": filename,
            "title": meeting_title,
        }

    thread = threading.Thread(
        target=process_audio_file,
        args=(
            file_path,
            job_id,
            transcribe_engine,
            meeting_title,
            original_filename,
        ),
    )
    thread.daemon = True
    thread.start()

    return jsonify({"job_id": job_id, "status": "started"})


@app.route("/status/<job_id>")
def job_status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)

    if job is None:
        return jsonify({"error": "Không tìm thấy job"}), 404

    # Không cần trả transcript/minutes đầy đủ trong lúc polling để tiết kiệm băng thông
    response = {"status": job["status"], "message": job["message"]}
    if job["status"] == "completed":
        response["transcript"] = job["transcript"]
        response["minutes"] = job["minutes"]

    return jsonify(response)


# Các helper/API nhỏ dưới đây chỉ phục vụ sidebar lịch sử và Markdown editor.
def _result_path(result_id: str) -> str | None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", result_id):
        return None
    return os.path.join(RESULTS_FOLDER, f"{result_id}.txt")


def _metadata_path(result_id: str) -> str | None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", result_id):
        return None
    return os.path.join(RESULTS_FOLDER, f"{result_id}.json")


def _read_result_metadata(result_id: str) -> dict:
    path = _metadata_path(result_id)
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as source:
            metadata = json.load(source)
        return metadata if isinstance(metadata, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_result_metadata(
    result_id: str,
    *,
    title: str,
    filename: str,
    engine: str,
) -> None:
    path = _metadata_path(result_id)
    if not path:
        raise ValueError("Meeting ID không hợp lệ")
    temporary_path = f"{path}.tmp"
    metadata = {
        "title": title,
        "filename": filename,
        "engine": engine,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(temporary_path, "w", encoding="utf-8", newline="\n") as output:
        json.dump(metadata, output, ensure_ascii=False, indent=2)
        output.write("\n")
    os.replace(temporary_path, path)


def _read_result(result_id: str) -> dict | None:
    path = _result_path(result_id)
    if not path or not os.path.isfile(path):
        return None

    with open(path, "r", encoding="utf-8") as source:
        content = source.read()
    marker = "=== TRANSCRIPT ĐẦY ĐỦ ==="
    summary_part, transcript = (
        content.split(marker, 1) if marker in content else (content, "")
    )
    summary = summary_part.replace("=== BIÊN BẢN HỌP ===", "", 1).strip()

    metadata = _read_result_metadata(result_id)
    title = str(metadata.get("title") or "").strip()
    if not title:
        title = f"Cuộc họp {datetime.fromtimestamp(os.path.getmtime(path)):%d/%m/%Y}"
        for line in summary.splitlines():
            candidate = line.strip().lstrip("#>").replace("**", "").strip(" *_`")
            if (
                candidate
                and candidate != "---"
                and candidate.upper() != "THÔNG BÁO"
            ):
                title = candidate[:180]
                break

    timestamp = datetime.fromtimestamp(
        os.path.getmtime(path), tz=timezone.utc
    ).isoformat()
    return {
        "id": result_id,
        "title": title,
        "filename": str(metadata.get("filename") or ""),
        "engine": str(metadata.get("engine") or ""),
        "created_at": str(metadata.get("created_at") or timestamp),
        "updated_at": timestamp,
        "summary": summary,
        "transcript": transcript.strip(),
    }


def _write_result(result_id: str, summary: str, transcript: str) -> None:
    path = _result_path(result_id)
    if not path:
        raise ValueError("Meeting ID không hợp lệ")
    temporary_path = f"{path}.tmp"
    with open(temporary_path, "w", encoding="utf-8", newline="\n") as output:
        output.write(
            "=== BIÊN BẢN HỌP ===\n\n"
            f"{summary}\n\n"
            "=== TRANSCRIPT ĐẦY ĐỦ ===\n\n"
            f"{transcript}"
        )
    os.replace(temporary_path, path)


@app.route("/api/meetings")
def meetings_index():
    meetings = []
    with results_lock:
        for filename in os.listdir(RESULTS_FOLDER):
            if filename.endswith(".txt"):
                meeting = _read_result(filename[:-4])
                if meeting:
                    meetings.append({
                        key: meeting[key]
                        for key in ("id", "title", "created_at", "updated_at")
                    })
    meetings.sort(key=lambda item: item["updated_at"], reverse=True)
    return jsonify({"meetings": meetings})


@app.route("/api/meetings/<result_id>")
def meeting_detail(result_id):
    with results_lock:
        meeting = _read_result(result_id)
    if not meeting:
        return jsonify({"error": "Không tìm thấy cuộc họp"}), 404
    return jsonify(meeting)


@app.route("/api/meetings/<result_id>", methods=["PUT"])
def update_meeting(result_id):
    payload = request.get_json(silent=True) or {}
    summary = payload.get("summary")
    transcript = payload.get("transcript")
    if not isinstance(summary, str) or not isinstance(transcript, str):
        return jsonify({"error": "Nội dung không hợp lệ"}), 400
    if len(summary) > 20_000_000 or len(transcript) > 20_000_000:
        return jsonify({"error": "Nội dung quá lớn"}), 413

    with results_lock:
        if not _read_result(result_id):
            return jsonify({"error": "Không tìm thấy cuộc họp"}), 404
        _write_result(result_id, summary, transcript)
        meeting = _read_result(result_id)
    return jsonify(meeting)


@app.route("/api/meetings/<result_id>/word")
def export_meeting_word(result_id):
    document_type = request.args.get("document", "summary")
    if document_type not in {"summary", "transcript"}:
        return jsonify({"error": "Loại tài liệu không hợp lệ"}), 400

    with results_lock:
        meeting = _read_result(result_id)
    if not meeting:
        return jsonify({"error": "Không tìm thấy cuộc họp"}), 404

    try:
        # Import lazily so transcription still starts with a clear error when
        # dependencies have not yet been installed from requirements.txt.
        from document_export import export_markdown_to_docx

        output = io.BytesIO()
        export_markdown_to_docx(
            meeting[document_type],
            output,
            title=meeting["title"],
            document_type=document_type,
        )
        output.seek(0)
    except ImportError as error:
        return jsonify({
            "error": "Thiếu python-docx. Hãy chạy: pip install -r requirements.txt"
        }), 500
    except Exception as error:
        return jsonify({"error": f"Không thể tạo file Word: {error}"}), 500

    safe_title = re.sub(
        r'[<>:"/\\|?*\x00-\x1f]+', "_", meeting["title"]
    ).strip(" ._")[:80] or "cuoc-hop"
    return send_file(
        output,
        as_attachment=True,
        download_name=f"{safe_title}.{document_type}.docx",
        mimetype=(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        max_age=0,
    )


def parse_args():
    parser = argparse.ArgumentParser(description="Run the AI Meeting Note Taker.")
    parser.add_argument(
        "--api",
        action="store_true",
        help=f"Use the Gemini API ({GEMINI_MODEL}) for meeting summaries.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    app.config["USE_GEMINI_API"] = args.api
    if args.api:
        print(f"[API] Gemini enabled for summaries: {GEMINI_MODEL}")
    print("Meeting Note Taker đang chạy tại http://localhost:5001")
    app.run(host="0.0.0.0", port=5001, debug=False, use_reloader=False)
