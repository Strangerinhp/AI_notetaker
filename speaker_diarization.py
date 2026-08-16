"""Speaker diarization helpers shared by the supported ASR engines.

The pipeline follows the reference notebook's order:

1. concatenate uploaded recordings in upload order;
2. convert the whole meeting to mono 16 kHz PCM WAV with FFmpeg;
3. run pyannote Community-1 on the complete meeting;
4. discard diarization turns shorter than the configured threshold;
5. expose the normalized meeting audio and retained timeline to the ASR engine.

Heavy diarization dependencies are imported lazily so ordinary transcription
continues to work when diarization is disabled.
"""

from __future__ import annotations

import os
import subprocess
import threading
import warnings
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

from audio_utils import TARGET_SAMPLE_RATE, find_ffmpeg


DIARIZATION_MODEL = os.environ.get(
    "DIARIZATION_MODEL",
    "pyannote/speaker-diarization-community-1",
)
DEFAULT_MIN_TURN_SECONDS = 2.0
DEFAULT_ASR_PADDING_SECONDS = 0.05

_pipeline = None
_pipeline_lock = threading.Lock()
_inference_lock = threading.Lock()


class DiarizationError(RuntimeError):
    """Actionable speaker-diarization error safe to display in the UI."""


@dataclass(frozen=True)
class SpeakerTurn:
    """One retained speaker turn; ``path`` is only used by the legacy flow."""

    path: str
    start: float
    end: float
    speaker: str
    source_speaker: str

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass(frozen=True)
class DiarizationResult:
    """Retained turns plus diagnostics from the full diarization pass."""

    turns: list[SpeakerTurn]
    raw_turn_count: int
    removed_turn_count: int
    speaker_count: int
    meeting_audio_path: str = ""
    audio_duration: float = 0.0


def _hugging_face_token() -> str | None:
    return (
        os.environ.get("HF_TOKEN")
        or os.environ.get("HUGGINGFACE_TOKEN")
        or os.environ.get("HUGGINGFACE_ACCESS_TOKEN")
    )


def _load_pipeline():
    # MeetNote advertises local processing. Disable pyannote's optional
    # anonymous usage metrics unless the operator explicitly opts in.
    os.environ.setdefault("PYANNOTE_METRICS_ENABLED", "0")
    try:
        import torch
        # Pyannote warns when TorchCodec cannot find shared FFmpeg DLLs. This
        # module deliberately passes a preloaded waveform dictionary, so its
        # built-in decoder is not used and that warning is not actionable.
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=r"\s*torchcodec is not installed correctly.*",
            )
            from pyannote.audio import Pipeline
    except ImportError as error:
        raise DiarizationError(
            "Thiếu pyannote.audio. Hãy chạy: pip install -r requirements.txt"
        ) from error

    token = _hugging_face_token()
    try:
        pipeline = Pipeline.from_pretrained(
            DIARIZATION_MODEL,
            token=token,
        )
    except Exception as error:
        message = str(error).lower()
        if not token or any(
            marker in message
            for marker in ("401", "403", "gated", "restricted", "token")
        ):
            raise DiarizationError(
                "Không tải được model pyannote Community-1. Hãy chấp nhận điều "
                "khoản của model trên Hugging Face và đặt biến môi trường HF_TOKEN "
                "trong terminal dùng để chạy app."
            ) from error
        raise DiarizationError(f"Không tải được model diarization: {error}") from error

    if pipeline is None:
        raise DiarizationError(
            "Không tải được model pyannote Community-1. Hãy kiểm tra quyền truy cập "
            "model và biến môi trường HF_TOKEN."
        )

    requested_device = os.environ.get("DIARIZATION_DEVICE", "").strip().lower()
    if requested_device:
        if requested_device.startswith("cuda") and not torch.cuda.is_available():
            raise DiarizationError(
                "DIARIZATION_DEVICE yêu cầu CUDA nhưng PyTorch không nhận GPU CUDA."
            )
        device = torch.device(requested_device)
    else:
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    pipeline.to(device)
    print(f"[diarization] Model ready on {device}: {DIARIZATION_MODEL}")
    if device.type == "cpu":
        print(
            "[diarization] Warning: CPU diarization can be slow for long recordings."
        )
    return pipeline


def get_pipeline():
    """Load the pyannote pipeline once and reuse it across jobs."""
    global _pipeline
    if _pipeline is None:
        with _pipeline_lock:
            if _pipeline is None:
                print(f"[diarization] Loading model: {DIARIZATION_MODEL}")
                _pipeline = _load_pipeline()
    return _pipeline


def _run_ffmpeg(command: list[str], action: str) -> None:
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        check=False,
    )
    if result.returncode != 0:
        details = result.stderr.strip() or "FFmpeg returned no error details."
        raise DiarizationError(f"{action} thất bại: {details}")


def normalize_meeting_audio(
    audio_paths: list[str],
    output_path: str | os.PathLike[str],
    *,
    use_loudnorm: bool = False,
) -> str:
    """Join uploaded files in order and create one 16 kHz mono PCM WAV.

    Loudness normalization is intentionally disabled by default because the
    existing application previously produced worse transcripts after audio
    normalization. It remains available as an explicit code-level option.
    """
    input_paths = [os.path.abspath(os.fspath(path)) for path in audio_paths]
    if not input_paths:
        raise DiarizationError("Cần ít nhất một file audio để tách người nói.")
    for input_path in input_paths:
        if not os.path.isfile(input_path):
            raise FileNotFoundError(f"Audio file not found: {input_path}")

    output = os.path.abspath(os.fspath(output_path))
    os.makedirs(os.path.dirname(output), exist_ok=True)
    command = [
        find_ffmpeg(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
    ]
    for input_path in input_paths:
        command.extend(["-i", input_path])

    if len(input_paths) == 1:
        command.extend(["-map", "0:a:0"])
        if use_loudnorm:
            command.extend(["-af", "loudnorm=I=-23:LRA=11:TP=-2"])
    else:
        normalized_streams = []
        stream_labels = []
        for index in range(len(input_paths)):
            label = f"audio{index}"
            filters = (
                f"[{index}:a:0]aresample={TARGET_SAMPLE_RATE},"
                "aformat=sample_fmts=s16:channel_layouts=mono,"
                "asetpts=PTS-STARTPTS"
            )
            if use_loudnorm:
                filters += ",loudnorm=I=-23:LRA=11:TP=-2"
            normalized_streams.append(f"{filters}[{label}]")
            stream_labels.append(f"[{label}]")
        concat_filter = (
            "".join(stream_labels)
            + f"concat=n={len(input_paths)}:v=0:a=1[meeting]"
        )
        command.extend(
            [
                "-filter_complex",
                ";".join([*normalized_streams, concat_filter]),
                "-map",
                "[meeting]",
            ]
        )

    command.extend(
        [
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(TARGET_SAMPLE_RATE),
            "-c:a",
            "pcm_s16le",
            output,
        ]
    )
    _run_ffmpeg(command, "Chuẩn bị audio cho diarization")
    return output


def _speaker_constraints(
    num_speakers: int | None,
    min_speakers: int | None,
    max_speakers: int | None,
) -> dict[str, int]:
    values = {
        "num_speakers": num_speakers,
        "min_speakers": min_speakers,
        "max_speakers": max_speakers,
    }
    for name, value in values.items():
        if value is not None and (isinstance(value, bool) or int(value) < 1):
            raise ValueError(f"{name} must be a positive integer or None.")
    if num_speakers is not None and (
        min_speakers is not None or max_speakers is not None
    ):
        raise ValueError(
            "num_speakers cannot be combined with min_speakers/max_speakers."
        )
    if (
        min_speakers is not None
        and max_speakers is not None
        and min_speakers > max_speakers
    ):
        raise ValueError("min_speakers cannot be greater than max_speakers.")
    return {name: int(value) for name, value in values.items() if value is not None}


def _raw_turns(annotation) -> list[tuple[float, float, str]]:
    turns = []
    for segment, _track, speaker in annotation.itertracks(yield_label=True):
        start = max(0.0, float(segment.start))
        end = max(start, float(segment.end))
        if end > start:
            turns.append((start, end, str(speaker)))
    turns.sort(key=lambda item: (item[0], item[1], item[2]))
    return turns


def _friendly_speaker_names(
    turns: list[tuple[float, float, str]],
) -> dict[str, str]:
    names: dict[str, str] = {}
    for _start, _end, source_speaker in turns:
        if source_speaker not in names:
            names[source_speaker] = f"người nói {len(names) + 1}"
    return names


def _format_timestamp(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def format_speaker_turn(turn: SpeakerTurn, text: str) -> str:
    """Render one ASR result as a speaker-attributed transcript line."""
    normalized = " ".join(text.split()).strip()
    return (
        f"[{_format_timestamp(turn.start)} - {_format_timestamp(turn.end)}] "
        f"{turn.speaker}: {normalized}"
    )


def diarize_audio_files(
    audio_paths: list[str],
    work_dir: str | os.PathLike[str],
    *,
    min_turn_seconds: float = DEFAULT_MIN_TURN_SECONDS,
    num_speakers: int | None = None,
    min_speakers: int | None = None,
    max_speakers: int | None = None,
    asr_padding_seconds: float = DEFAULT_ASR_PADDING_SECONDS,
    write_turn_audio: bool = True,
) -> DiarizationResult:
    """Diarize the meeting and optionally write legacy per-turn ASR WAV files."""
    if not 0 <= min_turn_seconds <= 30:
        raise ValueError("min_turn_seconds must be between 0 and 30 seconds.")
    if not 0 <= asr_padding_seconds <= 2:
        raise ValueError("asr_padding_seconds must be between 0 and 2 seconds.")

    constraints = _speaker_constraints(
        num_speakers,
        min_speakers,
        max_speakers,
    )
    work_path = Path(work_dir).resolve()
    work_path.mkdir(parents=True, exist_ok=True)
    meeting_wav = normalize_meeting_audio(
        audio_paths,
        work_path / "meeting_16k_mono.wav",
    )

    audio, sample_rate = sf.read(
        meeting_wav,
        dtype="float32",
        always_2d=False,
    )
    if audio.ndim != 1 or sample_rate != TARGET_SAMPLE_RATE:
        raise DiarizationError(
            f"Audio diarization phải là mono {TARGET_SAMPLE_RATE} Hz; "
            f"nhận shape={audio.shape}, sample_rate={sample_rate}."
        )
    audio = np.ascontiguousarray(audio, dtype=np.float32)

    try:
        import torch
    except ImportError as error:
        raise DiarizationError(
            "Thiếu PyTorch, không thể chạy speaker diarization."
        ) from error

    waveform = torch.from_numpy(audio).unsqueeze(0)
    pipeline = get_pipeline()
    print(
        "[diarization] Running full-meeting clustering with constraints: "
        f"{constraints or 'automatic'}"
    )
    try:
        with _inference_lock:
            output = pipeline(
                {"waveform": waveform, "sample_rate": sample_rate},
                **constraints,
            )
    except Exception as error:
        raise DiarizationError(f"Speaker diarization thất bại: {error}") from error

    annotation = getattr(output, "speaker_diarization", output)
    raw_turns = _raw_turns(annotation)
    if not raw_turns:
        raise DiarizationError("Pyannote không tìm thấy lượt nói nào trong audio.")

    retained = [
        turn for turn in raw_turns if turn[1] - turn[0] >= min_turn_seconds
    ]
    if not retained:
        raise DiarizationError(
            "Không có lượt nói nào vượt qua ngưỡng tối thiểu "
            f"{min_turn_seconds:.1f} giây. Hãy giảm ngưỡng trên giao diện."
        )

    speaker_names = _friendly_speaker_names(retained)
    audio_duration = audio.size / sample_rate
    turn_dir = work_path / "speaker_turns"
    if write_turn_audio:
        turn_dir.mkdir(parents=True, exist_ok=True)
    speaker_turns = []
    for index, (start, end, source_speaker) in enumerate(retained, start=1):
        turn_path = ""
        if write_turn_audio:
            padded_start = max(0.0, start - asr_padding_seconds)
            padded_end = min(audio_duration, end + asr_padding_seconds)
            first_sample = int(round(padded_start * sample_rate))
            final_sample = int(round(padded_end * sample_rate))
            segment_audio = audio[first_sample:final_sample]
            if segment_audio.size == 0:
                continue
            turn_path = str(turn_dir / f"turn_{index:05d}.wav")
            sf.write(
                turn_path,
                segment_audio,
                sample_rate,
                subtype="PCM_16",
            )
        speaker_turns.append(
            SpeakerTurn(
                path=turn_path,
                start=start,
                end=end,
                speaker=speaker_names[source_speaker],
                source_speaker=source_speaker,
            )
        )

    if not speaker_turns:
        raise DiarizationError("Không thể tạo audio cho các lượt nói đã nhận diện.")

    result = DiarizationResult(
        turns=speaker_turns,
        raw_turn_count=len(raw_turns),
        removed_turn_count=len(raw_turns) - len(retained),
        speaker_count=len(speaker_names),
        meeting_audio_path=str(meeting_wav),
        audio_duration=audio_duration,
    )
    print(
        "[diarization] Complete: "
        f"{result.speaker_count} speakers, {len(result.turns)} retained turns, "
        f"{result.removed_turn_count} turns shorter than "
        f"{min_turn_seconds:.1f}s removed."
    )
    return result
