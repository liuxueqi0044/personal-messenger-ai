from collections.abc import Callable
from pathlib import Path

from ..domain.events import EventEnvelope


class JsonlRecorder:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def append(self, event: EventEnvelope):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(event.model_dump_json() + "\n")


class JsonlReplay:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def events(self):
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                yield EventEnvelope.model_validate_json(line)

    def run(self, consumer: Callable):
        return [consumer(event) for event in self.events()]
