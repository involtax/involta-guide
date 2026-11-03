import json
import math
import os
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

from .config import Settings
from .errors import VoiceError


class AudioStorage:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.root = settings.data_dir / "audio"

    def save(
        self, content: bytes, mime: str, min_duration_seconds: float = 1, *, name: str | None = None
    ) -> tuple[str, float]:
        formats = {
            "audio/webm": ("webm", content[:4] == b"\x1aE\xdf\xa3"),
            "audio/ogg": ("ogg", content[:4] == b"OggS"),
            "audio/mp4": ("mp4", content[4:8] == b"ftyp"),
            "audio/wav": ("wav", content[:4] == b"RIFF" and content[8:12] == b"WAVE"),
        }
        if len(content) > self.settings.max_audio_bytes:
            raise VoiceError("Запись больше 10 МБ", "too_large", 413)
        if len(content) < 100 or mime not in formats or not formats[mime][1]:
            raise VoiceError("Неподдерживаемая аудиозапись")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if shutil.disk_usage(self.root).free < self.settings.min_free_bytes + len(content):
            raise VoiceError("Недостаточно места для надёжного сохранения", "storage", 503)
        name = name or f"{uuid4()}.{formats[mime][0]}"
        self.path(name)
        temporary = self.root / (name + ".upload")
        target = self.root / name
        try:
            with temporary.open("xb") as stream:
                os.chmod(temporary, 0o600)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            probe = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    "a:0",
                    "-show_entries",
                    "format=duration:stream=codec_type,duration,sample_rate:frame=nb_samples",
                    "-of",
                    "json",
                    str(temporary),
                ],
                capture_output=True,
                timeout=20,
                check=True,
            )
            info = json.loads(probe.stdout)
            if not any(s.get("codec_type") == "audio" for s in info.get("streams", [])):
                raise VoiceError("Аудиопоток не найден")
            stream = info["streams"][0]
            durations = [
                float(info.get("format", {}).get("duration") or 0),
                float(stream.get("duration") or 0),
            ]
            samples = sum(int(frame.get("nb_samples") or 0) for frame in info.get("frames", []))
            if samples and stream.get("sample_rate"):
                durations.append(samples / int(stream["sample_rate"]))
            duration = min((d for d in durations if d > 0 and math.isfinite(d)), default=0)
            if min_duration_seconds == 5 and duration < 5:
                raise VoiceError(
                    "Слишком коротко. Запиши хотя бы 5 секунд: назови ресторан и расскажи, что думаешь.",
                    "audio_too_short",
                    422,
                    min_duration_seconds=5,
                    duration_seconds=duration,
                )
            if not min_duration_seconds <= duration <= 121:
                raise VoiceError("Длительность должна быть от 1 до 120 секунд")
            os.replace(temporary, target)
            descriptor = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            return name, duration
        except VoiceError:
            raise
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise VoiceError("Не удалось сохранить/проверить запись", "storage", 503) from exc
        finally:
            temporary.unlink(missing_ok=True)

    def path(self, name: str) -> Path:
        if not name or Path(name).name != name:
            raise VoiceError("Недопустимое имя файла")
        return self.root / name
