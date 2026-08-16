"""Small SQL Server storage layer for MeetNote."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pyodbc


SERVER = os.environ.get("SQLSERVER_SERVER", ".")
DATABASE = os.environ.get("SQLSERVER_DATABASE", "MeetNote")
DRIVER = os.environ.get("SQLSERVER_DRIVER", "ODBC Driver 18 for SQL Server")
USERNAME = os.environ.get("SQLSERVER_USERNAME", "")
PASSWORD = os.environ.get("SQLSERVER_PASSWORD", "")


class DatabaseError(RuntimeError):
    """Database error with an actionable message safe to show in the UI."""


def _connection_string() -> str:
    parts = [
        f"DRIVER={{{DRIVER}}}",
        f"SERVER={SERVER}",
        f"DATABASE={DATABASE}",
    ]
    if USERNAME:
        parts.extend([f"UID={USERNAME}", f"PWD={PASSWORD}"])
    else:
        parts.append("Trusted_Connection=yes")
    parts.extend([
        "Encrypt=no",
        "TrustServerCertificate=yes",
    ])
    return ";".join(parts) + ";"


def _friendly_error(error: pyodbc.Error) -> DatabaseError:
    message = str(error)
    if "IM002" in message:
        detail = f"Không tìm thấy {DRIVER}. Hãy cài Microsoft ODBC Driver 18."
    elif "4060" in message or "Cannot open database" in message:
        detail = "Database MeetNote chưa tồn tại. Hãy chạy file database.sql trong SSMS."
    elif "Login failed" in message or "28000" in message:
        detail = (
            "SQL Server từ chối đăng nhập. Hãy thử Windows Authentication trong SSMS "
            "hoặc kiểm tra SQLSERVER_USERNAME/SQLSERVER_PASSWORD."
        )
    else:
        detail = f"Không kết nối hoặc truy vấn được SQL Server: {message}"
    return DatabaseError(detail)


def get_connection():
    try:
        return pyodbc.connect(_connection_string(), timeout=10)
    except pyodbc.Error as error:
        raise _friendly_error(error) from error


def _iso_utc(value):
    if not isinstance(value, datetime):
        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _row_to_meeting(cursor, row):
    if row is None:
        return None
    values = {
        column[0].lower(): value
        for column, value in zip(cursor.description, row)
    }
    if "id" in values:
        values["id"] = str(values["id"]).lower()
    for key in (
        "created_at",
        "completed_at",
        "last_edited_at",
        "word_updated_at",
        "updated_at",
    ):
        if key in values:
            values[key] = _iso_utc(values.get(key))
    if "has_word_document" in values:
        values["has_word_document"] = bool(values["has_word_document"])
    raw_segments = values.pop("diarization_segments_json", "[]") or "[]"
    try:
        values["diarization_segments"] = json.loads(raw_segments)
    except (TypeError, ValueError):
        values["diarization_segments"] = []
    return values


def check_database() -> None:
    connection = get_connection()
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT TOP (1) Id FROM dbo.MeetingHistory")
        cursor.fetchone()
    except pyodbc.Error as error:
        raise _friendly_error(error) from error
    finally:
        cursor.close()
        connection.close()


def insert_meeting(
    job_id,
    *,
    title,
    filename,
    transcript="",
    minutes="",
    engine="",
    status="queued",
    status_message="Đang chờ xử lý...",
    file_count=1,
    total_audio_bytes=0,
    created_at=None,
    completed_at=None,
):
    connection = get_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO dbo.MeetingHistory
            (
                Id, Title, FileName, Engine, Transcript, Minutes,
                Status, StatusMessage, FileCount, TotalAudioBytes,
                CreatedAt, CompletedAt, UpdatedAt
            )
            VALUES
            (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                COALESCE(?, SYSUTCDATETIME()), ?, COALESCE(?, SYSUTCDATETIME())
            )
            """,
            str(job_id),
            title,
            filename,
            engine,
            transcript,
            minutes,
            status,
            status_message,
            file_count,
            total_audio_bytes,
            created_at,
            completed_at,
            completed_at or created_at,
        )
        connection.commit()
    except pyodbc.Error as error:
        connection.rollback()
        raise _friendly_error(error) from error
    finally:
        cursor.close()
        connection.close()


def update_meeting_status(job_id, status, message):
    connection = get_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            """
            UPDATE dbo.MeetingHistory
            SET Status = ?, StatusMessage = ?, UpdatedAt = SYSUTCDATETIME()
            WHERE Id = ?
            """,
            status,
            message[:500],
            str(job_id),
        )
        connection.commit()
        return cursor.rowcount > 0
    except pyodbc.Error as error:
        connection.rollback()
        raise _friendly_error(error) from error
    finally:
        cursor.close()
        connection.close()


def complete_transcription(job_id, transcript, diarization_segments=None):
    """Persist ASR output while leaving summarization for explicit user action."""
    segments_json = json.dumps(
        diarization_segments or [],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    connection = get_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            """
            UPDATE dbo.MeetingHistory
            SET Transcript = ?, DiarizationSegments = ?,
                Status = 'transcript_ready',
                StatusMessage = N'Transcript đã sẵn sàng để kiểm tra.',
                UpdatedAt = SYSUTCDATETIME()
            WHERE Id = ?
            """,
            transcript,
            segments_json,
            str(job_id),
        )
        connection.commit()
        return cursor.rowcount > 0
    except pyodbc.Error as error:
        connection.rollback()
        raise _friendly_error(error) from error
    finally:
        cursor.close()
        connection.close()


def complete_meeting(
    job_id,
    transcript,
    minutes,
    word_file_path=None,
    word_filename=None,
):
    connection = get_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            """
            UPDATE dbo.MeetingHistory
            SET Transcript = ?, Minutes = ?, WordFilePath = ?, WordFileName = ?,
                WordUpdatedAt = CASE WHEN ? = 0 THEN WordUpdatedAt
                                     ELSE SYSUTCDATETIME() END,
                Status = 'completed',
                StatusMessage = N'Hoàn tất!',
                CompletedAt = SYSUTCDATETIME(), UpdatedAt = SYSUTCDATETIME()
            WHERE Id = ?
            """,
            transcript,
            minutes,
            word_file_path,
            word_filename,
            int(word_file_path is not None),
            str(job_id),
        )
        connection.commit()
        return cursor.rowcount > 0
    except pyodbc.Error as error:
        connection.rollback()
        raise _friendly_error(error) from error
    finally:
        cursor.close()
        connection.close()


def get_meeting(job_id):
    connection = get_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            """
            SELECT
                CONVERT(nvarchar(36), Id) AS id,
                Title AS title,
                FileName AS filename,
                Engine AS engine,
                Transcript AS transcript,
                Minutes AS summary,
                CAST(CASE WHEN WordFilePath IS NULL THEN 0 ELSE 1 END AS bit)
                    AS has_word_document,
                WordFileName AS word_filename,
                WordUpdatedAt AS word_updated_at,
                DiarizationSegments AS diarization_segments_json,
                Status AS status,
                StatusMessage AS status_message,
                FileCount AS file_count,
                TotalAudioBytes AS total_audio_bytes,
                CreatedAt AS created_at,
                CompletedAt AS completed_at,
                LastEditedAt AS last_edited_at,
                UpdatedAt AS updated_at
            FROM dbo.MeetingHistory
            WHERE Id = ?
            """,
            str(job_id),
        )
        return _row_to_meeting(cursor, cursor.fetchone())
    except pyodbc.Error as error:
        raise _friendly_error(error) from error
    finally:
        cursor.close()
        connection.close()


def get_meetings():
    connection = get_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            """
            SELECT
                CONVERT(nvarchar(36), Id) AS id,
                Title AS title,
                Status AS status,
                CreatedAt AS created_at,
                LastEditedAt AS last_edited_at,
                UpdatedAt AS updated_at
            FROM dbo.MeetingHistory
            WHERE Status IN ('transcript_ready', 'summarizing', 'summary_error', 'completed')
            ORDER BY UpdatedAt DESC
            """
        )
        return [
            _row_to_meeting(cursor, row)
            for row in cursor.fetchall()
        ]
    except pyodbc.Error as error:
        raise _friendly_error(error) from error
    finally:
        cursor.close()
        connection.close()


def update_meeting(job_id, transcript, minutes=None, diarization_segments=None):
    segments_json = (
        json.dumps(
            diarization_segments,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if diarization_segments is not None
        else None
    )
    connection = get_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            """
            UPDATE dbo.MeetingHistory
            SET Transcript = ?, Minutes = COALESCE(?, Minutes),
                DiarizationSegments = COALESCE(?, DiarizationSegments),
                LastEditedAt = SYSUTCDATETIME(),
                UpdatedAt = SYSUTCDATETIME()
            WHERE Id = ? AND Status IN (
                'transcript_ready', 'summarizing', 'summary_error', 'completed'
            )
            """,
            transcript,
            minutes,
            segments_json,
            str(job_id),
        )
        connection.commit()
        found = cursor.rowcount > 0
    except pyodbc.Error as error:
        connection.rollback()
        raise _friendly_error(error) from error
    finally:
        cursor.close()
        connection.close()
    return get_meeting(job_id) if found else None


def get_word_document(job_id):
    """Return private summary DOCX metadata without reading file contents."""
    connection = get_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            """
            SELECT WordFilePath, WordFileName, WordUpdatedAt
            FROM dbo.MeetingHistory
            WHERE Id = ?
            """,
            str(job_id),
        )
        row = cursor.fetchone()
        if row is None or row[0] is None:
            return None
        return {
            "relative_path": row[0],
            "filename": row[1] or "bao-cao-cuoc-hop.docx",
            "updated_at": _iso_utc(row[2]),
        }
    except pyodbc.Error as error:
        raise _friendly_error(error) from error
    finally:
        cursor.close()
        connection.close()


def update_word_document(job_id, relative_path: str, filename: str):
    """Point a completed meeting at a replacement report file."""
    connection = get_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            """
            UPDATE dbo.MeetingHistory
            SET WordFilePath = ?, WordFileName = ?,
                WordUpdatedAt = SYSUTCDATETIME(),
                LastEditedAt = SYSUTCDATETIME(),
                UpdatedAt = SYSUTCDATETIME()
            WHERE Id = ? AND Status = 'completed'
            """,
            relative_path,
            filename,
            str(job_id),
        )
        connection.commit()
        found = cursor.rowcount > 0
    except pyodbc.Error as error:
        connection.rollback()
        raise _friendly_error(error) from error
    finally:
        cursor.close()
        connection.close()
    return get_meeting(job_id) if found else None


def store_generated_word_document(job_id, relative_path: str, filename: str):
    """Point a meeting at a generated report without marking a user edit."""
    connection = get_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            """
            UPDATE dbo.MeetingHistory
            SET WordFilePath = ?, WordFileName = ?,
                WordUpdatedAt = SYSUTCDATETIME()
            WHERE Id = ? AND Status = 'completed'
            """,
            relative_path,
            filename,
            str(job_id),
        )
        connection.commit()
        found = cursor.rowcount > 0
    except pyodbc.Error as error:
        connection.rollback()
        raise _friendly_error(error) from error
    finally:
        cursor.close()
        connection.close()
    return get_meeting(job_id) if found else None
