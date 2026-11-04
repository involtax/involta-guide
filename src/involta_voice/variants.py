"""The three GigaAM beta recognitions; only the original GigaAM publishes text."""

SHERPA_STAGES = {
    "sherpa_gigaam_plain": ("gigaam", "sherpa_plain"),
    "sherpa_gigaam_hotwords": ("gigaam", "sherpa_hotwords"),
}
STAGES = ("transcription", *SHERPA_STAGES)
RESULTS = (
    ("gigaam", "onnx"),
    *SHERPA_STAGES.values(),
)
VARIANT_LABELS = {
    "onnx": "onnx-asr",
    "sherpa_plain": "Sherpa · без словаря",
    "sherpa_hotwords": "Sherpa · со словарём",
}


def result_identity(kind: str, primary: str = "gigaam") -> tuple[str, str]:
    if kind in SHERPA_STAGES:
        return SHERPA_STAGES[kind]
    if kind == "transcription":
        return primary, "onnx"
    if kind == "comparison":
        return ("parakeet" if primary == "gigaam" else "gigaam"), "onnx"
    if kind.startswith("evaluate_"):
        return kind.removeprefix("evaluate_"), "onnx"
    raise ValueError("Неизвестный вариант распознавания")


def dictionary_names(names: list[str]) -> list[str]:
    return sorted({" ".join(name.split()) for name in names if name.strip()})
