"""Five Sherpa profiles; the frozen recommended profile publishes text."""

# score, search width, dictionary source. Frozen in each run at intake.
SHERPA_PROFILES = {
    "sherpa_plain": (5.0, 8, "none"),
    "sherpa_hotwords_weak": (4.5, 8, "catalogue"),
    "sherpa_hotwords": (5.0, 8, "catalogue"),
    "sherpa_hotwords_strong": (5.5, 8, "catalogue"),
    "sherpa_hotwords_wide": (5.0, 16, "catalogue"),
}
SHERPA_STAGES = {
    "sherpa_gigaam_" + variant.removeprefix("sherpa_"): ("gigaam", variant)
    for variant in SHERPA_PROFILES
}
STAGES = ("transcription", *SHERPA_STAGES, "sherpa_gigaam_hotwords_city")
RESULTS = tuple(SHERPA_STAGES.values())
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
    if kind == "sherpa_gigaam_hotwords_city":
        return "gigaam", "sherpa_hotwords_city"
    if kind == "transcription":
        return primary, "onnx"
    if kind == "comparison":
        return ("parakeet" if primary == "gigaam" else "gigaam"), "onnx"
    if kind.startswith("evaluate_"):
        return kind.removeprefix("evaluate_"), "onnx"
    raise ValueError("Неизвестный вариант распознавания")


def dictionary_names(names: list[str]) -> list[str]:
    return sorted({" ".join(name.split()) for name in names if name.strip()})


def primary_variant(snapshot: dict[str, object]) -> str:
    return str(snapshot.get("primary_variant", "onnx"))


def primary_kind(snapshot: dict[str, object]) -> str:
    variant = primary_variant(snapshot)
    if variant == "onnx":
        return "transcription"
    return next(kind for kind, (_, value) in SHERPA_STAGES.items() if value == variant)
