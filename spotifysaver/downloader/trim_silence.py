"""FFmpeg post-processing for trimming silence from audio downloads."""

import os
import re
import subprocess
from pathlib import Path

from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessor
from yt_dlp.utils import PostProcessingError

SILENCE_FILTER = (
    "silenceremove=start_periods=1:start_duration=0.1:"
    "start_threshold=-50dB,areverse,"
    "silenceremove=start_periods=1:start_duration=0.1:"
    "start_threshold=-50dB,areverse"
)


class FFmpegTrimSilencePP(FFmpegPostProcessor):
    """Trim leading and trailing silence from a downloaded audio file."""

    _CODECS = {
        "m4a": "aac",
        "mp3": "libmp3lame",
        "opus": "libopus",
    }

    def __init__(self, downloader, output_format: str, bitrate: int):
        super().__init__(downloader)
        self.output_format = output_format
        self.bitrate = bitrate
        self._last_ffmpeg_diagnostic: str | None = None

    @staticmethod
    def _escape_ffmetadata(value: str) -> str:
        return (
            value.replace("\\", "\\\\")
            .replace("=", "\\=")
            .replace(";", "\\;")
            .replace("#", "\\#")
            .replace("\n", "\\n")
        )

    def _leading_silence_duration(self, filepath: Path) -> float:
        command = [
            self.executable,
            "-hide_banner",
            "-i",
            str(filepath),
            "-af",
            "silencedetect=noise=-50dB:d=0.1",
            "-f",
            "null",
            "-",
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        diagnostic = result.stderr or ""
        self.write_debug(diagnostic)
        if result.returncode:
            raise PostProcessingError(
                f"Could not inspect leading silence: "
                f"{diagnostic.strip().splitlines()[-1] if diagnostic.strip() else 'FFmpeg failed'}"
            )

        match = re.search(r"silence_end:\s*([0-9]+(?:\.[0-9]+)?)", diagnostic)
        return float(match.group(1)) if match else 0.0

    def _chapter_metadata(
        self, chapters: list[dict], leading_silence: float, duration: float
    ) -> str:
        lines = [";FFMETADATA1"]
        for chapter in chapters:
            start = max(0.0, float(chapter["start_time"]) - leading_silence)
            end = min(duration, float(chapter["end_time"]) - leading_silence)
            if end <= start:
                continue

            lines.extend(
                [
                    "",
                    "[CHAPTER]",
                    "TIMEBASE=1/1000",
                    f"START={round(start * 1000)}",
                    f"END={round(end * 1000)}",
                ]
            )
            for key, value in (chapter.get("tags") or {}).items():
                lines.append(
                    f"{self._escape_ffmetadata(str(key))}="
                    f"{self._escape_ffmetadata(str(value))}"
                )
        return "\n".join(lines) + "\n"

    def write_debug(self, text: str, *args, **kwargs) -> None:
        if text and not text.startswith("ffmpeg command line:"):
            self._last_ffmpeg_diagnostic = text
        super().write_debug(text, *args, **kwargs)

    def run(self, information: dict) -> tuple[list[str], dict]:
        filepath = Path(information["filepath"])
        self._last_ffmpeg_diagnostic = None
        codec = self._CODECS.get(self.output_format)
        if not codec:
            raise PostProcessingError(
                f"Unsupported audio format for silence trimming: {self.output_format}"
            )

        temporary_path = filepath.with_name(
            f"{filepath.stem}.trim-audio{filepath.suffix}"
        )
        muxed_path = filepath.with_name(f"{filepath.stem}.trim-final{filepath.suffix}")
        metadata_path = filepath.with_name(f"{filepath.stem}.chapters.ffmeta")
        try:
            original_metadata = self.get_metadata_object(
                str(filepath), ["-show_chapters"]
            )
            chapters = original_metadata.get("chapters") or []
            leading_silence = self._leading_silence_duration(filepath)
            self.run_ffmpeg(
                str(filepath),
                str(temporary_path),
                [
                    "-map",
                    "0:a:0",
                    "-map_metadata",
                    "0",
                    "-map_chapters",
                    "-1",
                    "-c",
                    "copy",
                    "-c:a",
                    codec,
                    "-b:a",
                    f"{self.bitrate}k",
                    "-af",
                    SILENCE_FILTER,
                ],
            )
            if chapters:
                trimmed_metadata = self.get_metadata_object(str(temporary_path))
                duration = float(trimmed_metadata["format"]["duration"])
                metadata_path.write_text(
                    self._chapter_metadata(chapters, leading_silence, duration),
                    encoding="utf-8",
                )
                self.real_run_ffmpeg(
                    [
                        (str(temporary_path), []),
                        (str(metadata_path), ["-f", "ffmetadata"]),
                    ],
                    [
                        (
                            str(muxed_path),
                            [
                                "-map",
                                "0:a:0",
                                "-map_metadata",
                                "0",
                                "-map_chapters",
                                "1",
                                "-c",
                                "copy",
                            ],
                        )
                    ],
                )
                os.replace(muxed_path, filepath)
            else:
                os.replace(temporary_path, filepath)
        except Exception as error:
            for temp_file in (temporary_path, muxed_path, metadata_path):
                if temp_file.exists():
                    temp_file.unlink()
            diagnostic = self._last_ffmpeg_diagnostic
            if diagnostic:
                diagnostic_tail = "\n".join(diagnostic.strip().splitlines()[-12:])
                raise PostProcessingError(
                    f"Silence trimming failed: {error}\n{diagnostic_tail}"
                ) from error
            raise

        if temporary_path.exists():
            temporary_path.unlink()
        if metadata_path.exists():
            metadata_path.unlink()
        information["filepath"] = str(filepath)
        return [], information
