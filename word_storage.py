"""Private filesystem storage for generated and uploaded meeting reports."""

from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_WORD_STORAGE_FOLDER = BASE_DIR / "word_documents"


class WordStorageError(RuntimeError):
    """Raised when a report path is unsafe or its file cannot be stored."""


def get_word_storage_folder() -> Path:
    """Return the configured private report root as an absolute path."""
    configured = os.environ.get("WORD_STORAGE_FOLDER", "").strip()
    folder = Path(configured).expanduser() if configured else DEFAULT_WORD_STORAGE_FOLDER
    if not folder.is_absolute():
        folder = BASE_DIR / folder
    return folder.resolve()


def _safe_job_id(job_id: str) -> str:
    try:
        return str(uuid.UUID(str(job_id)))
    except (TypeError, ValueError, AttributeError) as error:
        raise WordStorageError("ID cuộc họp không hợp lệ để lưu file Word.") from error


def resolve_word_path(relative_path: str, *, must_exist: bool = False) -> Path:
    """Resolve a database path while preventing access outside the report root."""
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise WordStorageError("Đường dẫn file Word đang trống.")

    root = get_word_storage_folder()
    supplied = Path(relative_path.strip())
    if supplied.is_absolute():
        raise WordStorageError("Đường dẫn file Word phải là đường dẫn tương đối.")
    candidate = (root / supplied).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise WordStorageError("Đường dẫn file Word nằm ngoài thư mục lưu trữ.") from error
    if candidate.suffix.lower() != ".docx":
        raise WordStorageError("Đường dẫn báo cáo phải trỏ tới file DOCX.")
    if must_exist and not candidate.is_file():
        raise WordStorageError("File Word đã lưu không còn tồn tại trên máy chủ.")
    return candidate


def write_word_document(job_id: str, data: bytes) -> str:
    """Atomically write a DOCX and return its portable relative path."""
    if not isinstance(data, bytes) or not data:
        raise WordStorageError("Không có dữ liệu Word để lưu.")

    safe_job_id = _safe_job_id(job_id)
    relative_path = f"{safe_job_id}/{uuid.uuid4()}.docx"
    target = resolve_word_path(relative_path)
    target.parent.mkdir(parents=True, exist_ok=True)

    temporary_path = None
    try:
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=".meetnote-",
            suffix=".tmp",
            dir=target.parent,
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(file_descriptor, "wb") as temporary_file:
            temporary_file.write(data)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, target)
    except OSError as error:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise WordStorageError(f"Không thể lưu file Word trên máy chủ: {error}") from error
    return relative_path


def remove_word_document(relative_path: str | None) -> None:
    """Remove one known report file, never an arbitrary or recursive path."""
    if not relative_path:
        return
    try:
        target = resolve_word_path(relative_path)
        target.unlink(missing_ok=True)
        try:
            target.parent.rmdir()
        except OSError:
            pass
    except (OSError, WordStorageError):
        # Cleanup must never hide the successful database/job update.
        pass
