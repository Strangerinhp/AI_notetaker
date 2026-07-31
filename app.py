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
import os
import re
import shutil
import threading
import uuid
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template, request
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


def process_audio_file(file_path: str, job_id: str, transcribe_engine: str = "whisper") -> None:
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

        update_job(job_id, status="summarizing", message="Đang tóm tắt thành biên bản họp...")
        minutes = summarize_transcript(
            strip_transcript_timestamps(transcript),
            model=OLLAMA_MODEL,
            use_gemini_api=app.config["USE_GEMINI_API"],
        )

        # Lưu kết quả ra file để tiện tải về / xem lại sau
        result_path = os.path.join(RESULTS_FOLDER, f"{job_id}.txt")
        with open(result_path, "w", encoding="utf-8") as f:
            f.write("=== BIÊN BẢN HỌP ===\n\n")
            f.write(minutes)
            f.write("\n\n=== TRANSCRIPT ĐẦY ĐỦ ===\n\n")
            f.write(transcript)

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

    job_id = str(uuid.uuid4())
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
        }

    thread = threading.Thread(target=process_audio_file, args=(file_path, job_id, transcribe_engine))
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

    title = f"Cuộc họp {datetime.fromtimestamp(os.path.getmtime(path)):%d/%m/%Y}"
    for line in summary.splitlines():
        candidate = line.strip().lstrip("#>").replace("**", "").strip(" *_`")
        if candidate and candidate != "---":
            title = candidate[:120]
            break

    timestamp = datetime.fromtimestamp(
        os.path.getmtime(path), tz=timezone.utc
    ).isoformat()
    return {
        "id": result_id,
        "title": title,
        "filename": "",
        "engine": "",
        "created_at": timestamp,
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
