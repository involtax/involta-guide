"""The six explicit beta recognitions; only the original GigaAM publishes text."""

SHERPA_STAGES = {
    "sherpa_gigaam_plain": ("gigaam", "sherpa_plain"),
    "sherpa_gigaam_hotwords": ("gigaam", "sherpa_hotwords"),
    "sherpa_parakeet_plain": ("parakeet", "sherpa_plain"),
    "sherpa_parakeet_hotwords": ("parakeet", "sherpa_hotwords"),
}
STAGES = ("transcription", "comparison", *SHERPA_STAGES)
RESULTS = (
    ("gigaam", "onnx"),
    ("parakeet", "onnx"),
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
