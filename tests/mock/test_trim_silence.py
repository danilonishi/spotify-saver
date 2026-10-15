import json
import math
import shutil
import struct
import subprocess
import wave

import pytest
import yt_dlp
from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessorError
from yt_dlp.utils import PostProcessingError

from spotifysaver.downloader.trim_silence import FFmpegTrimSilencePP


def test_trim_silence_postprocessor_removes_leading_and_trailing_silence(tmp_path):
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("ffmpeg and ffprobe are required for this postprocessor test")

    sample_rate = 44100
    silence_start = int(sample_rate * 0.6)
    tone_samples = int(sample_rate * 1.0)
    silence_middle = int(sample_rate * 0.5)
    silence_end = int(sample_rate * 0.7)
    samples = [0] * silence_start
    samples.extend(
        int(12000 * math.sin(2 * math.pi * 440 * sample / sample_rate))
        for sample in range(tone_samples)
    )
    samples.extend([0] * silence_middle)
    samples.extend(
        int(12000 * math.sin(2 * math.pi * 440 * sample / sample_rate))
        for sample in range(tone_samples)
    )
    samples.extend([0] * silence_end)
    stereo_samples = b"".join(
        struct.pack("<hh", sample, sample) for sample in samples
    )

    wav_path = tmp_path / "source.wav"
    with wave.open(str(wav_path), "wb") as audio_file:
        audio_file.setnchannels(2)
        audio_file.setsampwidth(2)
        audio_file.setframerate(sample_rate)
        audio_file.writeframes(stereo_samples)

    cover_path = tmp_path / "cover.jpg"
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=64x64",
            "-frames:v",
            "1",
            str(cover_path),
        ],
        check=True,
        capture_output=True,
    )
    audio_path = tmp_path / "source.m4a"
    chapters_path = tmp_path / "chapters.txt"
    chapters_path.write_text(
        ";FFMETADATA1\n"
        "[CHAPTER]\n"
        "TIMEBASE=1/1000\n"
        "START=0\n"
        "END=3800\n"
        "title=Whole track\n",
        encoding="utf-8",
    )
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-y",
            "-i",
            str(wav_path),
            "-i",
            str(cover_path),
            "-i",
            str(chapters_path),
            "-map",
            "0:a:0",
            "-map",
            "1:v:0",
            "-map_metadata",
            "2",
            "-map_chapters",
            "2",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-c:v",
            "mjpeg",
            "-disposition:v",
            "attached_pic",
            "-metadata",
            "title=Silence Trim Test",
            str(audio_path),
        ],
        check=True,
        capture_output=True,
    )

    with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
        processor = FFmpegTrimSilencePP(ydl, output_format="m4a", bitrate=128)
        _, information = processor.run(
            {
                "filepath": str(audio_path),
                "ext": "m4a",
                "acodec": "aac",
            }
        )

    assert information["filepath"] == str(audio_path)
    assert audio_path.is_file()
    probe = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration:format_tags=title:stream=codec_type",
            "-of",
            "json",
            "-show_chapters",
            str(audio_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    probe_result = json.loads(probe.stdout)
    result = probe_result["format"]
    duration = float(result["duration"])
    assert 2.2 <= duration < 2.8
    assert result["tags"]["title"] == "Silence Trim Test"
    streams = probe_result["streams"]
    assert all(stream["codec_type"] != "video" for stream in streams)
    assert len(probe_result["chapters"]) == 1
    chapter = probe_result["chapters"][0]
    assert chapter["tags"]["title"] == "Whole track"
    assert float(chapter["start_time"]) <= 0.02
    assert float(chapter["end_time"]) <= duration + 0.02


def test_trim_silence_failure_includes_ffmpeg_diagnostic_and_keeps_source(tmp_path):
    audio_path = tmp_path / "source.mp3"
    audio_path.write_bytes(b"source audio")

    with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
        processor = FFmpegTrimSilencePP(ydl, output_format="mp3", bitrate=128)
    processor.get_metadata_object = lambda *_args: {"chapters": []}
    processor._leading_silence_duration = lambda _filepath: 0.0

    def fail_conversion(*_args):
        processor.write_debug(
            "Stream mapping:\n"
            "Could not find audio stream\n"
            "Conversion failed!"
        )
        raise FFmpegPostProcessorError("Conversion failed!")

    processor.run_ffmpeg = fail_conversion

    with pytest.raises(PostProcessingError, match="Could not find audio stream"):
        processor.run({"filepath": str(audio_path), "ext": "mp3"})

    assert audio_path.read_bytes() == b"source audio"
    assert not (tmp_path / "source.trim.mp3").exists()
