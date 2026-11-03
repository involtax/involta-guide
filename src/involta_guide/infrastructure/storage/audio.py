"""Guide-local legacy file deletion for the explicit beta transition/reset only."""

from pathlib import Path

from involta_guide.domain.entities import RuleViolation


class FileAudioStorage:
    def __init__(self, root: Path, max_bytes: int) -> None:
        self.root = root / "audio"

    def path(self, name: str) -> Path:
        if Path(name).name != name or not name:
            raise RuleViolation("Недопустимое имя файла")
        return self.root / name

    def delete(self, name: str) -> None:
        self.path(name).unlink(missing_ok=True)
