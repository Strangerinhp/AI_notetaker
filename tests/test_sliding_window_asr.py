import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from sliding_window_asr import (
    TimedUnit,
    assign_units_to_turns,
    build_sliding_windows,
    window_owns,
)
from speaker_diarization import SpeakerTurn
import app as meetnote
import speaker_diarization
import transcribe_nghiasr
import transcribe_whisper


class SlidingWindowTests(unittest.TestCase):
    def test_windows_have_overlap_but_non_overlapping_keep_zones(self):
        windows = build_sliding_windows(
            61,
            window_seconds=30,
            overlap_seconds=5,
        )

        self.assertEqual(
            [(item.start, item.end) for item in windows],
            [(0, 30), (25, 55), (50, 61)],
        )
        self.assertEqual(
            [(item.keep_start, item.keep_end) for item in windows],
            [(0, 27.5), (27.5, 52.5), (52.5, 61)],
        )

    def test_overlap_token_is_owned_by_exactly_one_window(self):
        windows = build_sliding_windows(40, window_seconds=30, overlap_seconds=5)
        token = TimedUnit("word", 28, 28)

        self.assertEqual(sum(window_owns(token, item) for item in windows), 1)

    def test_units_choose_maximum_overlap_then_nearest_turn(self):
        turns = [
            SimpleNamespace(start=0, end=10),
            SimpleNamespace(start=10, end=20),
        ]
        units = [
            TimedUnit("first", 2, 3),
            TimedUnit("second", 12, 14),
            TimedUnit("nearest", 20.2, 20.2),
        ]

        assignments = assign_units_to_turns(units, turns)

        self.assertEqual([unit.text for unit in assignments[0]], ["first"])
        self.assertEqual(
            [unit.text for unit in assignments[1]],
            ["second", "nearest"],
        )


class NghiAsrSlidingIntegrationTests(unittest.TestCase):
    @patch.object(transcribe_nghiasr.sf, "read")
    @patch.object(transcribe_nghiasr, "_recognize_samples")
    def test_overlap_hypotheses_are_deduplicated_before_speaker_alignment(
        self,
        recognize,
        read_audio,
    ):
        read_audio.return_value = (np.zeros(40 * 16000, dtype=np.float32), 16000)
        recognize.side_effect = [
            SimpleNamespace(
                tokens=["▁một", "▁lặp"],
                timestamps=[26.0, 28.0],
                text="một lặp",
            ),
            SimpleNamespace(
                tokens=["▁lặp", "▁hai"],
                timestamps=[3.0, 5.0],
                text="lặp hai",
            ),
        ]
        turns = [
            SpeakerTurn("", 0, 27.5, "người nói 1", "SPEAKER_00"),
            SpeakerTurn("", 27.5, 40, "người nói 2", "SPEAKER_01"),
        ]
        progress = []

        transcript = transcribe_nghiasr.transcribe_diarized_audio(
            "meeting.wav",
            turns,
            progress_callback=lambda current, total: progress.append((current, total)),
            window_seconds=30,
            overlap_seconds=5,
        )

        self.assertIn("người nói 1: Một", transcript)
        self.assertIn("người nói 2: Lặp hai", transcript)
        self.assertEqual(transcript.lower().count("lặp"), 1)
        self.assertEqual(progress, [(1, 2), (2, 2)])


class WhisperSlidingIntegrationTests(unittest.TestCase):
    @patch.object(transcribe_whisper.sf, "read")
    @patch.object(transcribe_whisper, "get_model")
    def test_whisper_word_timestamps_deduplicate_overlap(self, get_model, read_audio):
        read_audio.return_value = (np.zeros(40 * 16000, dtype=np.float32), 16000)
        model = Mock()
        model.device = SimpleNamespace(type="cpu")
        model.transcribe.side_effect = [
            {
                "segments": [{
                    "start": 26.0,
                    "end": 28.5,
                    "text": " một lặp",
                    "words": [
                        {"word": " một", "start": 26.0, "end": 26.4},
                        {"word": " lặp", "start": 28.0, "end": 28.4},
                    ],
                }]
            },
            {
                "segments": [{
                    "start": 3.0,
                    "end": 5.5,
                    "text": " lặp hai",
                    "words": [
                        {"word": " lặp", "start": 3.0, "end": 3.4},
                        {"word": " hai", "start": 5.0, "end": 5.4},
                    ],
                }]
            },
        ]
        get_model.return_value = model
        turns = [
            SpeakerTurn("", 0, 27.5, "người nói 1", "SPEAKER_00"),
            SpeakerTurn("", 27.5, 40, "người nói 2", "SPEAKER_01"),
        ]

        transcript = transcribe_whisper.transcribe_diarized_audio(
            "meeting.wav",
            turns,
            window_seconds=30,
            overlap_seconds=5,
        )

        self.assertIn("người nói 1: một", transcript.lower())
        self.assertIn("người nói 2: lặp hai", transcript.lower())
        self.assertEqual(transcript.lower().count("lặp"), 1)
        self.assertTrue(all(call.kwargs["word_timestamps"] for call in model.transcribe.call_args_list))


class AppSlidingPipelineTests(unittest.TestCase):
    def setUp(self):
        meetnote.app.config["USE_DATABASE"] = False
        with meetnote.jobs_lock:
            meetnote.jobs.clear()
            meetnote.jobs["job-1"] = {
                "status": "queued",
                "message": "",
                "title": "Cuộc họp",
                "transcript": "",
                "minutes": "",
                "created_at": "2026-08-14T00:00:00+00:00",
            }

    @patch.object(meetnote, "cleanup_files")
    @patch.object(meetnote.shutil, "rmtree")
    @patch.object(meetnote, "transcribe_whisper_diarized")
    @patch.object(speaker_diarization, "diarize_audio_files")
    def test_app_uses_full_meeting_audio_instead_of_turn_files(
        self,
        diarize,
        transcribe,
        remove_tree,
        cleanup,
    ):
        turns = [SpeakerTurn("", 0, 8, "người nói 1", "SPEAKER_00")]
        diarize.return_value = SimpleNamespace(
            turns=turns,
            speaker_count=1,
            audio_duration=65.0,
            meeting_audio_path="meeting_16k_mono.wav",
        )
        transcribe.return_value = (
            "[00:00:00 - 00:00:08] người nói 1: nội dung"
        )

        meetnote.process_audio_files(
            ["upload.wav"],
            "job-1",
            "whisper",
            diarization_enabled=True,
        )

        self.assertTrue(diarize.call_args.kwargs["write_turn_audio"] is False)
        self.assertEqual(transcribe.call_args.args, ("meeting_16k_mono.wav", turns))
        self.assertEqual(meetnote.jobs["job-1"]["status"], "transcript_ready")
        self.assertIn("nội dung", meetnote.jobs["job-1"]["transcript"])


if __name__ == "__main__":
    unittest.main()
