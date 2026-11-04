from involta_voice.text import restore_names


def test_dictionary_spelling_changes_only_complete_exact_names():
    assert (
        restore_names(
            "плёс, плюс, плёс равно плёс в квадрате. В усадьба плёс и ГРАНДЪ СОВА.",
            ["Плёс", "Грандъ Сова", "Усадьба Плёс"],
        )
        == "Плёс, плюс, Плёс равно Плёс в квадрате. В Усадьба Плёс и Грандъ Сова."
    )
    assert restore_names("в плёсе и плёсовый", ["Плёс"]) == "в плёсе и плёсовый"
    assert restore_names("плюс и плес", ["Плёс"]) == "плюс и плес"
    assert restore_names("до грандъ   сова!", ["Грандъ Сова"]) == "до Грандъ Сова!"
    assert restore_names("слова", []) == "слова"
