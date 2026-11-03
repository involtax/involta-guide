import json
import math
import subprocess
from pathlib import Path
from uuid import uuid4

from involta_guide.domain.entities import AudioTooShort, RuleViolation


class FileAudioStorage:
    def __init__(self, root: Path, max_bytes: int) -> None:
        self.root = root / "audio"
        self.max_bytes = max_bytes

    def save(self, content: bytes, mime: str) -> tuple[str, float]:
        types = {
            "audio/webm": ("webm", content[:4] == b"\x1aE\xdf\xa3"),
            "audio/ogg": ("ogg", content[:4] == b"OggS"),
            "audio/mp4": ("mp4", content[4:8] == b"ftyp"),
            "audio/wav": ("wav", content[:4] == b"RIFF" and content[8:12] == b"WAVE"),
        }
        if not 100 <= len(content) <= self.max_bytes or mime not in types or not types[mime][1]:
            raise RuleViolation("Нужен поддерживаемый аудиофайл до 10 МБ")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        name = f"{uuid4()}.{types[mime][0]}"
        path = self.root / name
        try:
            with path.open("xb") as file:
                file.write(content)
            path.chmod(0o600)
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
                    str(path),
                ],
                capture_output=True,
                timeout=20,
                check=True,
            )
            info = json.loads(probe.stdout)
            if not any(stream.get("codec_type") == "audio" for stream in info.get("streams", [])):
                raise RuleViolation("Аудиопоток не найден")
            stream = info["streams"][0]
            durations = [
                float(info.get("format", {}).get("duration") or 0),
                float(stream.get("duration") or 0),
            ]
            samples = sum(int(frame.get("nb_samples") or 0) for frame in info.get("frames", []))
            if samples and stream.get("sample_rate"):
                durations.append(samples / int(stream["sample_rate"]))
            duration = min((d for d in durations if d > 0 and math.isfinite(d)), default=0)
            if 0 < duration < 1:
                raise AudioTooShort(duration)
            if not math.isfinite(duration) or not 1 <= duration <= 121:
                raise RuleViolation("Реальная длительность записи должна быть от 1 до 120 секунд")
            return name, duration
        except (subprocess.SubprocessError, ValueError, OSError, RuleViolation) as exc:
            path.unlink(missing_ok=True)
            if isinstance(exc, RuleViolation):
                raise
            raise RuleViolation("Не удалось проверить аудиозапись") from exc

    def path(self, name: str) -> Path:
        if Path(name).name != name or not name:
            raise RuleViolation("Недопустимое имя файла")
        return self.root / name

    def read(self, name: str) -> bytes:
        return self.path(name).read_bytes()

    def delete(self, name: str) -> None:
        self.path(name).unlink(missing_ok=True)
