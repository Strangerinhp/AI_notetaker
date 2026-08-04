"""Audio conversion and chunking helpers.

This module intentionally performs no denoising, loudness normalization, VAD,
or silence removal.  FFmpeg only converts the first audio stream to mono
16 kHz PCM WAV and splits it into continuous chunks for transcription.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path


TARGET_SAMPLE_RATE = 16_000


def _find_ffmpeg():
    """Return an FFmpeg executable path."""
    configured = os.environ.get("FFMPEG_BINARY")
    if configured:
        configured_path = Path(configured).expanduser()
        if configured_path.is_file():
            return str(configured_path)

        resolved = shutil.which(configured)
        if resolved:
            return resolved

        raise RuntimeError(
            f"FFMPEG_BINARY points to {configured!r}, but that executable "
            "could not be found."
        )

    resolved = shutil.which("ffmpeg")
    if resolved:
        return resolved

    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError):
        pass

    raise RuntimeError(
        "FFmpeg was not found. Install FFmpeg and add it to PATH, or set "
        "FFMPEG_BINARY to the full path of ffmpeg.exe."
    )


def _chunk_sort_key(path):
    match = re.search(r"_part(\d+)\.wav$", os.path.basename(path))
    return int(match.group(1)) if match else 0


def split_audio(audio_path, temp_dir, segment_minutes=10):
    """Convert and split audio in one FFmpeg pass.

    This preserves the old preprocessing behavior (mono, 16 kHz WAV) without
    loading the complete recording into memory through pydub.
    """
    if segment_minutes <= 0:
        raise ValueError("segment_minutes must be greater than zero.")

    input_path = os.path.abspath(os.fspath(audio_path))
    if not os.path.isfile(input_path):
        raise FileNotFoundError(f"Audio file not found: {input_path}")

    output_dir = os.path.abspath(os.fspath(temp_dir))
    os.makedirs(output_dir, exist_ok=True)

    # A percent sign has special meaning in FFmpeg output patterns.
    base_name = Path(input_path).stem.replace("%", "_")
    output_pattern = os.path.join(output_dir, f"{base_name}_part%03d.wav")
    segment_seconds = float(segment_minutes) * 60
    segment_time = f"{segment_seconds:.6f}".rstrip("0").rstrip(".")

    command = [
        _find_ffmpeg(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        input_path,
        "-map",
        "0:a:0",
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(TARGET_SAMPLE_RATE),
        "-c:a",
        "pcm_s16le",
        "-f",
        "segment",
        "-segment_time",
        segment_time,
        "-segment_start_number",
        "1",
        "-reset_timestamps",
        "1",
        output_pattern,
    ]

    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        creationflags=creation_flags,
        check=False,
    )
    if result.returncode != 0:
        details = result.stderr.strip() or "FFmpeg returned no error details."
        raise RuntimeError(f"Audio splitting failed: {details}")

    prefix = f"{base_name}_part"
    chunk_paths = [
        os.path.join(output_dir, file_name)
        for file_name in os.listdir(output_dir)
        if file_name.startswith(prefix)
        and file_name.endswith(".wav")
        and file_name[len(prefix) : -4].isdigit()
    ]
    chunk_paths.sort(key=_chunk_sort_key)

    if not chunk_paths:
        raise RuntimeError(
            "Audio splitting failed: FFmpeg did not create any WAV chunks."
        )
    return chunk_paths


def split_audio_files(audio_paths, temp_dir, segment_minutes=10):
    """Concatenate audio files in order, then split the combined stream.

    Every input is decoded and normalized to mono 16 kHz PCM before FFmpeg's
    concat filter joins it.  Segmentation happens in the same FFmpeg pass, so
    no large intermediate combined recording is written to disk.
    """
    if segment_minutes <= 0:
        raise ValueError("segment_minutes must be greater than zero.")

    input_paths = [os.path.abspath(os.fspath(path)) for path in audio_paths]
    if not input_paths:
        raise ValueError("At least one audio file is required.")
    for input_path in input_paths:
        if not os.path.isfile(input_path):
            raise FileNotFoundError(f"Audio file not found: {input_path}")

    if len(input_paths) == 1:
        return split_audio(
            input_paths[0],
            temp_dir,
            segment_minutes=segment_minutes,
        )

    output_dir = os.path.abspath(os.fspath(temp_dir))
    os.makedirs(output_dir, exist_ok=True)
    output_prefix = "combined_part"
    output_pattern = os.path.join(output_dir, f"{output_prefix}%03d.wav")
    segment_seconds = float(segment_minutes) * 60
    segment_time = f"{segment_seconds:.6f}".rstrip("0").rstrip(".")

    command = [
        _find_ffmpeg(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
    ]
    for input_path in input_paths:
        command.extend(["-i", input_path])

    normalized_streams = []
    stream_labels = []
    for index in range(len(input_paths)):
        label = f"audio{index}"
        normalized_streams.append(
            f"[{index}:a:0]aresample={TARGET_SAMPLE_RATE},"
            f"aformat=sample_fmts=s16:channel_layouts=mono,"
            f"asetpts=PTS-STARTPTS[{label}]"
        )
        stream_labels.append(f"[{label}]")
    concat_filter = (
        "".join(stream_labels)
        + f"concat=n={len(input_paths)}:v=0:a=1[combined]"
    )

    command.extend([
        "-filter_complex",
        ";".join([*normalized_streams, concat_filter]),
        "-map",
        "[combined]",
        "-vn",
        "-c:a",
        "pcm_s16le",
        "-f",
        "segment",
        "-segment_time",
        segment_time,
        "-segment_start_number",
        "1",
        "-reset_timestamps",
        "1",
        output_pattern,
    ])

    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        creationflags=creation_flags,
        check=False,
    )
    if result.returncode != 0:
        details = result.stderr.strip() or "FFmpeg returned no error details."
        raise RuntimeError(f"Audio concatenation and splitting failed: {details}")

    chunk_paths = [
        os.path.join(output_dir, file_name)
        for file_name in os.listdir(output_dir)
        if file_name.startswith(output_prefix)
        and file_name.endswith(".wav")
        and file_name[len(output_prefix) : -4].isdigit()
    ]
    chunk_paths.sort(key=_chunk_sort_key)
    if not chunk_paths:
        raise RuntimeError(
            "Audio concatenation and splitting failed: FFmpeg did not create "
            "any WAV chunks."
        )
    return chunk_paths


def cleanup_files(paths):
    for path in paths:
        try:
            os.remove(path)
        except OSError:
            pass
