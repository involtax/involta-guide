"""One active Sherpa profile; saved requests retain their original identities."""

# score, search width, dictionary source. Frozen in each run at intake.
PRIMARY_VARIANT = "sherpa_hotwords_strong"
SHERPA_PROFILES = {PRIMARY_VARIANT: (5.5, 8, "catalogue")}
# Identities remain readable/executable for accepted jobs and saved history.
SHERPA_STAGES = {
    "sherpa_gigaam_" + variant.removeprefix("sherpa_"): ("gigaam", variant)
    for variant in (
        "sherpa_plain",
        "sherpa_hotwords_weak",
        "sherpa_hotwords",
        PRIMARY_VARIANT,
        "sherpa_hotwords_wide",
        "sherpa_hotwords_city",
    )
}
STAGES = ("transcription", *SHERPA_STAGES)
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
