"""
app.py
------
Web app self-hosted để ghi biên bản họp từ file audio:
  - Whisper/NghiASR/Zipformer -> chuyển giọng nói thành văn bản
  - Ollama hoặc Gemini        -> tóm tắt thành biên bản họp
  - FFmpeg                    -> chuẩn bị và chia nhỏ audio

Chạy:
    python app.py
Sau đó mở trình duyệt: http://localhost:5001
"""

import argparse
import importlib
import io
import math
import os
import re
import shutil
import threading
import uuid
from datetime import datetime, timezone

from flask import Flask, Response, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

from audio_utils import cleanup_files, split_audio_files
from database import (
    DatabaseError,
    check_database,
    complete_meeting,
    complete_transcription,
    delete_meeting as delete_database_meeting,
    get_meeting,
    get_meetings,
    get_word_document,
    insert_meeting,
    store_generated_word_document,
    update_meeting as save_meeting,
    update_meeting_status,
    update_word_document,
)
from summarize import (
    GEMINI_MODEL,
    MEETING_MINUTES_SYSTEM_PROMPT,
    summarize_transcript,
)
from sliding_window_asr import build_sliding_windows
from word_storage import (
    remove_word_document,
    resolve_word_path,
    write_word_document,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
TEMP_FOLDER = os.path.join(BASE_DIR, "temp_segments")

ALLOWED_EXTENSIONS = {"mp3", "wav", "m4a", "mp4", "ogg", "flac", "webm"}
MAX_WORD_DOCUMENT_BYTES = 25 * 1024 * 1024
MAX_SUMMARY_SYSTEM_PROMPT_CHARS = 30_000
SEGMENT_MINUTES = 2
NGHIASR_SEGMENT_MINUTES = 0.5
ZIPFORMER_SEGMENT_MINUTES = 0.5
DEFAULT_MIN_SPEAKER_TURN_SECONDS = 2.0
WHISPER_LANGUAGE = None  # Tự nhận diện để hỗ trợ cả cuộc họp tiếng Việt và tiếng Anh.
OLLAMA_MODEL = "qwen3.5:9b"  # đổi theo model bạn đã pull trong Ollama

TRANSCRIBE_ENGINES = {
    "whisper": {
        "label": "Whisper",
        "module": "transcribe_whisper",
        "segment_minutes": SEGMENT_MINUTES,
        "diarization": True,
    },
    "nghiasr": {
        "label": "NghiASR",
        "module": "transcribe_nghiasr",
        "segment_minutes": NGHIASR_SEGMENT_MINUTES,
        "diarization": True,
    },
    "zipformer": {
        "label": "Zipformer 30M",
        "module": "transcribe_zipformer",
        "segment_minutes": ZIPFORMER_SEGMENT_MINUTES,
        "diarization": True,
    },
}
TRANSCRIBE_ENGINE_LABELS = {
    engine: config["label"] for engine, config in TRANSCRIBE_ENGINES.items()
}
DIARIZATION_ENGINES = {
    engine
    for engine, config in TRANSCRIBE_ENGINES.items()
    if config["diarization"]
}

for folder in (UPLOAD_FOLDER, TEMP_FOLDER):
    os.makedirs(folder, exist_ok=True)

app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["USE_GEMINI_API"] = False
app.config["USE_DATABASE"] = True
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024  # tổng request tối đa 2 GB

# Trạng thái polling luôn nằm trong RAM. Khi --no-database được bật, cùng dict
# này cũng là kho tạm cho sidebar và transcript; DOCX chỉ nằm trên filesystem.
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def database_enabled() -> bool:
    return bool(app.config.get("USE_DATABASE", True))


def update_job(job_id: str, **kwargs) -> None:
    with jobs_lock:
        jobs[job_id].update(kwargs)
        jobs[job_id]["updated_at"] = utc_now()


def update_job_status(job_id: str, status: str, message: str) -> None:
    update_job(job_id, status=status, message=message)
    if database_enabled():
        update_meeting_status(job_id, status, message)


def _job_to_meeting(job_id: str, job: dict) -> dict:
    """Return the same public shape as database.get_meeting()."""
    return {
        "id": job_id,
        "title": job.get("title", ""),
        "filename": job.get("filename", ""),
        "engine": job.get("engine", ""),
        "transcript": job.get("transcript", ""),
        "summary": job.get("minutes", ""),
        "has_word_document": bool(job.get("word_file_path")),
        "word_filename": job.get("word_filename"),
        "word_updated_at": job.get("word_updated_at"),
        "word_storage_enabled": True,
        "diarization_segments": job.get("diarization_segments", []),
        "status": job.get("status", "queued"),
        "status_message": job.get("message", ""),
        "file_count": job.get("file_count", 1),
        "total_audio_bytes": job.get("total_audio_bytes", 0),
        "created_at": job.get("created_at"),
        "completed_at": job.get("completed_at"),
        "last_edited_at": job.get("last_edited_at"),
        "updated_at": job.get("updated_at"),
    }


def get_stored_meeting(job_id: str):
    if database_enabled():
        meeting = get_meeting(job_id)
        if meeting:
            meeting["word_storage_enabled"] = True
        return meeting
    with jobs_lock:
        job = jobs.get(job_id)
        return _job_to_meeting(job_id, job) if job else None


def get_stored_meetings() -> list[dict]:
    if database_enabled():
        return get_meetings()
    with jobs_lock:
        meetings = [
            _job_to_meeting(job_id, job)
            for job_id, job in jobs.items()
            if job.get("status")
            in {"transcript_ready", "summarizing", "summary_error", "completed"}
        ]
    meetings.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
    return [
        {
            key: meeting.get(key)
            for key in (
                "id",
                "title",
                "status",
                "created_at",
                "last_edited_at",
                "updated_at",
            )
        }
        for meeting in meetings
    ]


def delete_stored_meeting(job_id: str):
    """Delete meeting metadata first, then best-effort cleanup its DOCX file."""
    meeting = get_stored_meeting(job_id)
    if not meeting:
        return None, "missing"
    if meeting.get("status") == "summarizing":
        return None, "busy"

    if database_enabled():
        deleted = delete_database_meeting(job_id)
        if not deleted:
            remaining = get_meeting(job_id)
            return None, (
                "busy"
                if remaining and remaining.get("status") == "summarizing"
                else "missing"
            )
        relative_path = deleted.get("relative_path")
        with jobs_lock:
            jobs.pop(job_id, None)
    else:
        with jobs_lock:
            job = jobs.get(job_id)
            if not job:
                return None, "missing"
            if job.get("status") == "summarizing":
                return None, "busy"
            deleted = jobs.pop(job_id)
            relative_path = deleted.get("word_file_path")

    remove_word_document(relative_path)
    return True, None


def save_stored_meeting(
    job_id: str,
    transcript: str,
    minutes: str | None = None,
    diarization_segments: list[dict] | None = None,
):
    if database_enabled():
        meeting = save_meeting(job_id, transcript, minutes, diarization_segments)
        if meeting:
            meeting["word_storage_enabled"] = True
        return meeting
    with jobs_lock:
        job = jobs.get(job_id)
        if not job or job.get("status") not in {
            "transcript_ready",
            "summarizing",
            "summary_error",
            "completed",
        }:
            return None
        edited_at = utc_now()
        job.update(
            transcript=transcript,
            diarization_segments=(
                diarization_segments
                if diarization_segments is not None
                else job.get("diarization_segments", [])
            ),
            last_edited_at=edited_at,
            updated_at=edited_at,
        )
        if minutes is not None:
            job["minutes"] = minutes
        return _job_to_meeting(job_id, job)


def _safe_word_filename(title: str, filename: str | None = None) -> str:
    source = filename or f"{title}.docx"
    safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", source).strip(" ._")
    stem = safe_name[:-5] if safe_name.lower().endswith(".docx") else safe_name
    return f"{stem[:235] or 'bao-cao-cuoc-hop'}.docx"


def _create_word_document(markdown: str, title: str, document_type: str) -> bytes:
    from document_export import export_markdown_to_docx

    output = io.BytesIO()
    export_markdown_to_docx(
        markdown,
        output,
        title=title,
        document_type=document_type,
    )
    return output.getvalue()


def get_stored_word_document(job_id: str):
    if database_enabled():
        metadata = get_word_document(job_id)
    else:
        with jobs_lock:
            job = jobs.get(job_id)
            metadata = (
                {
                    "relative_path": job.get("word_file_path"),
                    "filename": job.get("word_filename"),
                    "updated_at": job.get("word_updated_at"),
                }
                if job and job.get("word_file_path")
                else None
            )
    if not metadata:
        return None
    return {
        **metadata,
        "path": resolve_word_path(metadata["relative_path"]),
    }


def save_stored_word_document(
    job_id: str,
    data: bytes,
    filename: str,
    *,
    mark_edited: bool = True,
):
    previous = get_stored_word_document(job_id)
    relative_path = write_word_document(job_id, data)
    meeting = None
    try:
        if database_enabled():
            writer = update_word_document if mark_edited else store_generated_word_document
            meeting = writer(job_id, relative_path, filename)
            if meeting:
                meeting["word_storage_enabled"] = True
        else:
            with jobs_lock:
                job = jobs.get(job_id)
                if job and job.get("status") == "completed":
                    stored_at = utc_now()
                    job.update(
                        word_file_path=relative_path,
                        word_filename=filename,
                        word_updated_at=stored_at,
                        updated_at=stored_at,
                    )
                    if mark_edited:
                        job["last_edited_at"] = stored_at
                    meeting = _job_to_meeting(job_id, job)
    except Exception:
        remove_word_document(relative_path)
        raise

    if not meeting:
        remove_word_document(relative_path)
        return None
    if previous and previous.get("relative_path") != relative_path:
        remove_word_document(previous.get("relative_path"))
    return meeting


def ensure_summary_word_document(job_id: str, meeting: dict):
    summary = meeting.get("summary", "")
    if not summary.strip():
        return None
    filename = _safe_word_filename(meeting["title"])

    stored = get_stored_word_document(job_id)
    if stored and stored["path"].is_file():
        return stored
    data = _create_word_document(summary, meeting["title"], "summary")
    saved = save_stored_word_document(
        job_id,
        data,
        filename,
        mark_edited=False,
    )
    if not saved:
        return get_stored_word_document(job_id)
    return get_stored_word_document(job_id)


def validate_diarization_segments(value) -> list[dict] | None:
    """Validate compact, path-free speaker timeline data received from the UI."""
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > 100_000:
        raise ValueError("Dữ liệu timeline người nói không hợp lệ.")

    clean_segments = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Dữ liệu timeline người nói không hợp lệ.")
        try:
            start = float(item.get("start"))
            end = float(item.get("end"))
        except (TypeError, ValueError) as error:
            raise ValueError("Mốc thời gian người nói không hợp lệ.") from error
        speaker = " ".join(str(item.get("speaker", "")).split())
        if (
            not math.isfinite(start)
            or not math.isfinite(end)
            or start < 0
            or end <= start
            or not speaker
            or len(speaker) > 200
        ):
            raise ValueError("Dữ liệu timeline người nói không hợp lệ.")
        clean_segments.append({
            "start": round(start, 3),
            "end": round(end, 3),
            "speaker": speaker,
        })
    return clean_segments


def strip_transcript_timestamps(transcript: str) -> str:
    """Remove display-only timeline labels before meeting summarization."""
    return re.sub(
        (
            r"^\[\d{2,}:\d{2}:\d{2}(?:\.\d{3})?"
            r"(?:\s*-\s*\d{2,}:\d{2}:\d{2}(?:\.\d{3})?)?\]\s*"
        ),
        "",
        transcript,
        flags=re.MULTILINE,
    )


def parse_diarization_options(form, transcribe_engine: str):
    """Validate optional diarization fields from an upload request."""
    enabled = str(form.get("diarization", "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if not enabled:
        return False, None, DEFAULT_MIN_SPEAKER_TURN_SECONDS
    if transcribe_engine not in DIARIZATION_ENGINES:
        raise ValueError("Model transcript đã chọn chưa hỗ trợ tách người nói.")

    speaker_count_text = str(form.get("speaker_count", "")).strip()
    speaker_count = None
    if speaker_count_text:
        try:
            speaker_count = int(speaker_count_text)
        except ValueError as error:
            raise ValueError("Số người nói phải là số nguyên từ 1 đến 20.") from error
        if not 1 <= speaker_count <= 20:
            raise ValueError("Số người nói phải nằm trong khoảng 1 đến 20.")

    min_turn_text = str(form.get("min_speaker_turn", "")).strip()
    try:
        min_turn_seconds = (
            float(min_turn_text)
            if min_turn_text
            else DEFAULT_MIN_SPEAKER_TURN_SECONDS
        )
    except ValueError as error:
        raise ValueError("Độ dài lượt nói tối thiểu phải là một số.") from error
    if not 0 <= min_turn_seconds <= 10:
        raise ValueError("Độ dài lượt nói tối thiểu phải từ 0 đến 10 giây.")
    return True, speaker_count, min_turn_seconds


def process_audio_files(
    file_paths: list[str],
    job_id: str,
    transcribe_engine: str = "zipformer",
    meeting_title: str = "Kết luận cuộc họp",
    diarization_enabled: bool = False,
    speaker_count: int | None = None,
    min_speaker_turn_seconds: float = DEFAULT_MIN_SPEAKER_TURN_SECONDS,
) -> None:
    """Run either continuous chunking or full-meeting speaker diarization."""
    segment_dir = os.path.join(TEMP_FOLDER, job_id)
    segment_paths = []

    try:
        diarization_result = None
        if diarization_enabled:
            update_job_status(
                job_id,
                "diarizing",
                "Đang ghép toàn bộ audio và nhận diện người nói...",
            )
            # Import lazily so the ordinary pipeline does not require DiariZen.
            from speaker_diarization import diarize_audio_files

            diarization_result = diarize_audio_files(
                file_paths,
                segment_dir,
                min_turn_seconds=min_speaker_turn_seconds,
                num_speakers=speaker_count,
                write_turn_audio=False,
            )
            segment_paths = [
                turn.path for turn in diarization_result.turns if turn.path
            ]
            total = len(build_sliding_windows(diarization_result.audio_duration))
        else:
            splitting_message = (
                f"Đang ghép {len(file_paths)} file và chia nhỏ audio..."
                if len(file_paths) > 1
                else "Đang chia nhỏ file audio..."
            )
            update_job_status(job_id, "splitting", splitting_message)
            segment_paths = split_audio_files(
                file_paths,
                segment_dir,
                segment_minutes=TRANSCRIBE_ENGINES[transcribe_engine][
                    "segment_minutes"
                ],
            )
            total = len(segment_paths)

        engine_label = TRANSCRIBE_ENGINE_LABELS[transcribe_engine]
        speaker_message = (
            f", {diarization_result.speaker_count} người nói"
            if diarization_result
            else ""
        )
        update_job_status(
            job_id,
            "transcribing",
            f"Đang chuyển giọng nói thành văn bản bằng {engine_label} "
            f"(0/{total}{speaker_message})...",
        )

        def on_progress(current, total_segments):
            update_job(
                job_id,
                message=f"Đang chuyển giọng nói thành văn bản bằng {engine_label} ({current}/{total_segments})...",
            )

        transcriber = importlib.import_module(
            TRANSCRIBE_ENGINES[transcribe_engine]["module"]
        )
        if diarization_result:
            transcript = transcriber.transcribe_diarized_audio(
                diarization_result.meeting_audio_path,
                diarization_result.turns,
                language=WHISPER_LANGUAGE,
                progress_callback=on_progress,
            )
        else:
            transcript = transcriber.transcribe_segments(
                segment_paths,
                language=WHISPER_LANGUAGE,
                progress_callback=on_progress,
            )

        # Một số engine (đặc biệt NghiASR) có thể trả về toàn bộ chữ in hoa.
        # Chuẩn hóa một lần tại đây để transcript hiển thị và lưu dưới dạng chữ thường.
        transcript = transcript.lower()

        diarization_segments = (
            [
                {
                    "start": round(turn.start, 3),
                    "end": round(turn.end, 3),
                    "speaker": turn.speaker.lower(),
                }
                for turn in diarization_result.turns
            ]
            if diarization_result
            else []
        )

        if database_enabled() and not complete_transcription(
            job_id,
            transcript,
            diarization_segments,
        ):
            raise DatabaseError("Không tìm thấy cuộc họp để lưu transcript.")

        update_job(
            job_id,
            status="transcript_ready",
            message="Transcript đã sẵn sàng để kiểm tra và chỉnh sửa.",
            transcript=transcript,
            minutes="",
            diarization_segments=diarization_segments,
        )

    except Exception as e:
        error_message = f"Lỗi: {e}"
        update_job(job_id, status="error", message=error_message)
        if database_enabled():
            try:
                update_meeting_status(job_id, "error", error_message)
            except DatabaseError:
                app.logger.exception("Không thể lưu trạng thái lỗi vào SQL Server")

    finally:
        # Dọn dẹp file tạm dù thành công hay lỗi
        cleanup_files(segment_paths)
        shutil.rmtree(segment_dir, ignore_errors=True)
        cleanup_files(file_paths)


def summarize_meeting(
    job_id: str,
    transcript: str,
    meeting_title: str,
    system_prompt: str | None = None,
) -> None:
    """Summarize the exact transcript revision submitted by the user."""
    try:
        minutes = summarize_transcript(
            strip_transcript_timestamps(transcript),
            model=OLLAMA_MODEL,
            use_gemini_api=app.config["USE_GEMINI_API"],
            meeting_title=meeting_title,
            system_prompt=system_prompt,
        )
        previous = get_stored_word_document(job_id)
        word_filename = _safe_word_filename(meeting_title)
        word_data = _create_word_document(minutes, meeting_title, "summary")
        word_file_path = write_word_document(job_id, word_data)
        storage_committed = False
        try:
            if database_enabled() and not complete_meeting(
                job_id,
                transcript,
                minutes,
                word_file_path,
                word_filename,
            ):
                raise DatabaseError("Không tìm thấy cuộc họp để lưu bản tóm tắt.")
            storage_committed = database_enabled()

            completed_at = utc_now()
            update_job(
                job_id,
                status="completed",
                message="Hoàn tất!",
                transcript=transcript,
                minutes=minutes,
                word_file_path=word_file_path,
                word_filename=word_filename,
                word_updated_at=completed_at,
                completed_at=completed_at,
            )
            storage_committed = True
        except Exception:
            if not storage_committed:
                remove_word_document(word_file_path)
            raise

        if previous and previous.get("relative_path") != word_file_path:
            remove_word_document(previous.get("relative_path"))
    except Exception as error:
        error_message = f"Tóm tắt thất bại: {error}"
        update_job(job_id, status="summary_error", message=error_message)
        if database_enabled():
            try:
                update_meeting_status(job_id, "summary_error", error_message)
            except DatabaseError:
                app.logger.exception("Không thể lưu lỗi tóm tắt vào SQL Server")


@app.route("/")
def index():
    return render_template(
        "index.html",
        default_summary_prompt=MEETING_MINUTES_SYSTEM_PROMPT,
    )


@app.errorhandler(DatabaseError)
def handle_database_error(error):
    app.logger.error("SQL Server error: %s", error)
    return jsonify({"error": str(error)}), 503


@app.route("/upload", methods=["POST"])
def upload_file():
    files = request.files.getlist("files")
    if not files:
        # Giữ tương thích với giao diện/client cũ chỉ gửi một trường "file".
        files = request.files.getlist("file")
    files = [file for file in files if file and file.filename]
    if not files:
        return jsonify({"error": "Chưa chọn file audio"}), 400

    unsupported_files = [
        file.filename for file in files if not allowed_file(file.filename)
    ]
    if unsupported_files:
        return jsonify({
            "error": (
                f"Định dạng file không được hỗ trợ: {', '.join(unsupported_files)}. "
                f"Hỗ trợ: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
            )
        }), 400

    meeting_title = " ".join(request.form.get("title", "").split())
    if not meeting_title:
        return jsonify({"error": "Vui lòng nhập tên báo cáo"}), 400
    if len(meeting_title) > 180:
        return jsonify({"error": "Tên báo cáo không được vượt quá 180 ký tự"}), 400

    transcribe_engine = request.form.get("engine", "zipformer")
    if transcribe_engine not in TRANSCRIBE_ENGINE_LABELS:
        transcribe_engine = "zipformer"
    try:
        diarization_enabled, speaker_count, min_speaker_turn_seconds = (
            parse_diarization_options(request.form, transcribe_engine)
        )
    except ValueError as error:
        return jsonify({"error": str(error)}), 400

    job_id = str(uuid.uuid4())
    original_filenames = [file.filename for file in files]
    original_filename = " → ".join(original_filenames)
    file_paths = []
    total_audio_bytes = 0
    try:
        for index, file in enumerate(files, start=1):
            extension = file.filename.rsplit(".", 1)[1].lower()
            filename = secure_filename(file.filename) or f"audio_{index}.{extension}"
            stored_name = f"{job_id}_{index:03d}_{filename}"
            file_path = os.path.join(app.config["UPLOAD_FOLDER"], stored_name)
            file.save(file_path)
            file_paths.append(file_path)
            total_audio_bytes += os.path.getsize(file_path)
    except OSError as error:
        cleanup_files(file_paths)
        return jsonify({"error": f"Không thể lưu file tải lên: {error}"}), 500

    stored_engine = (
        f"{transcribe_engine}+diarization"
        if diarization_enabled
        else transcribe_engine
    )
    if database_enabled():
        try:
            insert_meeting(
                job_id,
                title=meeting_title,
                filename=original_filename,
                engine=stored_engine,
                file_count=len(file_paths),
                total_audio_bytes=total_audio_bytes,
            )
        except DatabaseError:
            cleanup_files(file_paths)
            raise

    created_at = utc_now()
    with jobs_lock:
        jobs[job_id] = {
            "status": "queued",
            "message": "Đang chờ xử lý...",
            "filename": original_filename,
            "file_count": len(file_paths),
            "total_audio_bytes": total_audio_bytes,
            "title": meeting_title,
            "engine": stored_engine,
            "diarization": diarization_enabled,
            "transcript": "",
            "minutes": "",
            "word_file_path": None,
            "word_filename": None,
            "word_updated_at": None,
            "diarization_segments": [],
            "created_at": created_at,
            "completed_at": None,
            "last_edited_at": None,
            "updated_at": created_at,
        }

    thread = threading.Thread(
        target=process_audio_files,
        args=(
            file_paths,
            job_id,
            transcribe_engine,
            meeting_title,
            diarization_enabled,
            speaker_count,
            min_speaker_turn_seconds,
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

    # Không trả tài liệu lớn trong lúc model vẫn đang chạy để tiết kiệm băng thông.
    response = {"status": job["status"], "message": job["message"]}
    if job["status"] in {"transcript_ready", "summary_error", "completed"}:
        response["transcript"] = job["transcript"]
        response["minutes"] = job["minutes"]
        response["diarization_segments"] = job.get("diarization_segments", [])

    return jsonify(response)


@app.route("/api/meetings")
def meetings_index():
    return jsonify({"meetings": get_stored_meetings()})


@app.route("/api/meetings/<result_id>")
def meeting_detail(result_id):
    meeting = get_stored_meeting(result_id)
    if not meeting:
        return jsonify({"error": "Không tìm thấy cuộc họp"}), 404
    return jsonify(meeting)


@app.route("/api/meetings/<result_id>", methods=["DELETE"])
def delete_meeting_route(result_id):
    deleted, reason = delete_stored_meeting(result_id)
    if reason == "busy":
        return jsonify({
            "error": "Không thể xoá khi cuộc họp đang được tóm tắt."
        }), 409
    if not deleted:
        return jsonify({"error": "Không tìm thấy cuộc họp"}), 404
    return jsonify({"deleted": True, "id": result_id})


@app.route("/api/meetings/<result_id>", methods=["PUT"])
def update_meeting(result_id):
    payload = request.get_json(silent=True) or {}
    transcript = payload.get("transcript")
    if not isinstance(transcript, str):
        return jsonify({"error": "Nội dung không hợp lệ"}), 400
    if len(transcript) > 20_000_000:
        return jsonify({"error": "Nội dung quá lớn"}), 413
    try:
        diarization_segments = validate_diarization_segments(
            payload.get("diarization_segments")
        )
    except ValueError as error:
        return jsonify({"error": str(error)}), 400

    meeting = save_stored_meeting(
        result_id,
        transcript,
        None,
        diarization_segments,
    )
    if not meeting:
        return jsonify({"error": "Không tìm thấy cuộc họp"}), 404
    return jsonify(meeting)


@app.route("/api/meetings/<result_id>/summarize", methods=["POST"])
def summarize_saved_transcript(result_id):
    payload = request.get_json(silent=True) or {}
    transcript = payload.get("transcript")
    if not isinstance(transcript, str) or not transcript.strip():
        return jsonify({"error": "Transcript đang trống."}), 400
    if len(transcript) > 20_000_000:
        return jsonify({"error": "Nội dung quá lớn"}), 413
    system_prompt = payload.get("summary_prompt")
    if system_prompt is not None:
        if not isinstance(system_prompt, str) or not system_prompt.strip():
            return jsonify({"error": "Prompt tóm tắt không hợp lệ."}), 400
        system_prompt = system_prompt.strip()
        if len(system_prompt) > MAX_SUMMARY_SYSTEM_PROMPT_CHARS:
            return jsonify({"error": "Prompt tóm tắt vượt quá 30.000 ký tự."}), 413
    try:
        diarization_segments = validate_diarization_segments(
            payload.get("diarization_segments")
        )
    except ValueError as error:
        return jsonify({"error": str(error)}), 400

    meeting = get_stored_meeting(result_id)
    if not meeting:
        return jsonify({"error": "Không tìm thấy cuộc họp"}), 404
    if meeting.get("status") == "summarizing":
        return jsonify({"error": "Cuộc họp này đang được tóm tắt."}), 409
    if meeting.get("status") not in {
        "transcript_ready",
        "summary_error",
        "completed",
    }:
        return jsonify({"error": "Transcript chưa sẵn sàng để tóm tắt."}), 409

    meeting = save_stored_meeting(
        result_id,
        transcript,
        None,
        diarization_segments,
    )
    if not meeting:
        return jsonify({"error": "Không thể lưu transcript trước khi tóm tắt."}), 409

    message = "Đang tóm tắt transcript đã chỉnh sửa..."
    if database_enabled():
        update_meeting_status(result_id, "summarizing", message)

    with jobs_lock:
        cached = jobs.setdefault(result_id, {})
        cached.update({
            "title": meeting.get("title", "Kết luận cuộc họp"),
            "filename": meeting.get("filename", ""),
            "engine": meeting.get("engine", ""),
            "file_count": meeting.get("file_count", 1),
            "total_audio_bytes": meeting.get("total_audio_bytes", 0),
            "transcript": transcript,
            "minutes": meeting.get("summary", ""),
            "diarization_segments": (
                diarization_segments
                if diarization_segments is not None
                else meeting.get("diarization_segments", [])
            ),
            "status": "summarizing",
            "message": message,
            "created_at": meeting.get("created_at"),
            "completed_at": meeting.get("completed_at"),
            "last_edited_at": meeting.get("last_edited_at"),
            "updated_at": utc_now(),
        })

    thread = threading.Thread(
        target=summarize_meeting,
        args=(result_id, transcript, meeting["title"], system_prompt),
        daemon=True,
    )
    thread.start()
    return jsonify({"job_id": result_id, "status": "summarizing"}), 202


@app.route("/api/meetings/<result_id>/word")
def export_meeting_word(result_id):
    document_type = request.args.get("document", "summary")
    if document_type not in {"summary", "transcript"}:
        return jsonify({"error": "Loại tài liệu không hợp lệ"}), 400

    meeting = get_stored_meeting(result_id)
    if not meeting:
        return jsonify({"error": "Không tìm thấy cuộc họp"}), 404

    try:
        if document_type == "summary":
            stored = ensure_summary_word_document(result_id, meeting)
            if not stored:
                return jsonify({"error": "Cuộc họp chưa có báo cáo Word."}), 409
            source = stored["path"]
            download_name = _safe_word_filename(
                meeting["title"],
                stored.get("filename"),
            )
        else:
            data = _create_word_document(
                meeting["transcript"],
                meeting["title"],
                "transcript",
            )
            source = io.BytesIO(data)
            download_name = _safe_word_filename(
                meeting["title"],
                f"{meeting['title']}.transcript.docx",
            )
    except ImportError as error:
        return jsonify({
            "error": "Thiếu python-docx. Hãy chạy: pip install -r requirements.txt"
        }), 500
    except Exception as error:
        return jsonify({"error": f"Không thể tạo file Word: {error}"}), 500

    return send_file(
        source,
        as_attachment=True,
        download_name=download_name,
        mimetype=(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        max_age=0,
    )


@app.route("/api/meetings/<result_id>/word/view")
def view_meeting_word(result_id):
    meeting = get_stored_meeting(result_id)
    if not meeting:
        return jsonify({"error": "Không tìm thấy cuộc họp"}), 404
    try:
        stored = ensure_summary_word_document(result_id, meeting)
        if not stored:
            return jsonify({"error": "Cuộc họp chưa có báo cáo Word."}), 409
        from document_viewer import render_docx_html

        viewer_html = render_docx_html(
            stored["path"].read_bytes(),
            title=meeting["title"],
        )
    except ImportError:
        return jsonify({
            "error": "Thiếu python-docx. Hãy chạy: pip install -r requirements.txt"
        }), 500
    except Exception as error:
        return jsonify({"error": f"Không thể hiển thị file Word: {error}"}), 500

    response = Response(viewer_html, mimetype="text/html")
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'self'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.route("/api/meetings/<result_id>/word", methods=["POST"])
def replace_meeting_word(result_id):
    meeting = get_stored_meeting(result_id)
    if not meeting:
        return jsonify({"error": "Không tìm thấy cuộc họp"}), 404
    if meeting.get("status") != "completed":
        return jsonify({"error": "Chỉ có thể cập nhật Word sau khi tóm tắt hoàn tất."}), 409

    uploaded = request.files.get("word_file")
    if uploaded is None or not uploaded.filename:
        return jsonify({"error": "Hãy chọn một file DOCX."}), 400
    if not uploaded.filename.lower().endswith(".docx"):
        return jsonify({"error": "Chỉ hỗ trợ file có đuôi .docx."}), 400
    data = uploaded.stream.read(MAX_WORD_DOCUMENT_BYTES + 1)
    if len(data) > MAX_WORD_DOCUMENT_BYTES:
        return jsonify({"error": "File Word vượt quá giới hạn 25 MB."}), 413

    try:
        from document_viewer import InvalidWordDocument, validate_docx

        validate_docx(data)
    except ImportError:
        return jsonify({
            "error": "Thiếu python-docx. Hãy chạy: pip install -r requirements.txt"
        }), 500
    except InvalidWordDocument as error:
        return jsonify({"error": str(error)}), 400
    except Exception as error:
        return jsonify({"error": f"Không thể hiển thị file Word: {error}"}), 500

    filename = _safe_word_filename(meeting["title"], uploaded.filename)
    saved = save_stored_word_document(result_id, data, filename)
    if not saved:
        return jsonify({"error": "Không thể cập nhật file Word."}), 409
    return jsonify(saved)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Run the AI Meeting Note Taker.")
    parser.add_argument(
        "--api",
        action="store_true",
        help=f"Use the Gemini API ({GEMINI_MODEL}) for meeting summaries.",
    )
    parser.add_argument(
        "--no-database",
        action="store_true",
        help=(
            "Run without SQL Server. Meeting history and transcript edits stay "
            "in memory; Word reports are stored in WORD_STORAGE_FOLDER."
        ),
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = parse_args()
    app.config["USE_GEMINI_API"] = args.api
    app.config["USE_DATABASE"] = not args.no_database
    if database_enabled():
        try:
            check_database()
        except DatabaseError as error:
            print(f"[SQL Server] {error}")
            raise SystemExit(1) from error
    else:
        print(
            "[Database] Disabled: history and edits are temporary and will be "
            "lost when this process stops. Word files are generated on demand "
            "and are not stored."
        )
    if args.api:
        print(f"[API] Gemini enabled for summaries: {GEMINI_MODEL}")
    print("Meeting Note Taker đang chạy tại http://localhost:5001")
    app.run(host="0.0.0.0", port=5001, debug=False, use_reloader=False)
