"""The seven GigaAM beta profiles; only the original GigaAM publishes text."""

# score, search width, dictionary source. Frozen in each run at intake.
SHERPA_PROFILES = {
    "sherpa_plain": (1.5, 4, "none"),
    "sherpa_hotwords_weak": (0.75, 4, "catalogue"),
    "sherpa_hotwords": (1.5, 4, "catalogue"),
    "sherpa_hotwords_strong": (3.0, 4, "catalogue"),
    "sherpa_hotwords_wide": (1.5, 8, "catalogue"),
    "sherpa_hotwords_city": (1.5, 4, "city"),
}
SHERPA_STAGES = {
    "sherpa_gigaam_" + variant.removeprefix("sherpa_"): ("gigaam", variant)
    for variant in SHERPA_PROFILES
}
STAGES = ("transcription", *SHERPA_STAGES)
RESULTS = (("gigaam", "onnx"), *SHERPA_STAGES.values())
VARIANT_LABELS = {
    "onnx": "Базовый · onnx-asr",
    "sherpa_plain": "Sherpa · без словаря",
    "sherpa_hotwords_weak": "Sherpa · слабые подсказки",
    "sherpa_hotwords": "Sherpa · текущие подсказки",
    "sherpa_hotwords_strong": "Sherpa · сильные подсказки",
    "sherpa_hotwords_wide": "Sherpa · широкий поиск",
    "sherpa_hotwords_city": "Sherpa · только город",
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
