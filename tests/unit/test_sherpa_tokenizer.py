import hashlib

import pytest

from involta_voice.sherpa_tokenizer import VOCAB_SHA256, tokenizer, vocabulary


def test_native_tokenizer_has_real_piece_scores_and_distinguishes_plyos_plus(tmp_path):
    sp = tokenizer()
    assert sp.vocab_size() == 1024
    assert sp.encode("Плёс", out_type=str) == ["▁П", "лё", "с"]
    assert sp.encode("плёс", out_type=str) == ["▁п", "лё", "с"]
    assert sp.encode("плюс", out_type=str) == ["▁п", "лю", "с"]
    path = tmp_path / "tokens.txt"
    original = "".join(f"{sp.id_to_piece(i)} {i}\n" for i in range(sp.vocab_size()))
    path.write_text(original + "<blk> 1024\n")
    result = vocabulary(path)
    assert hashlib.sha256(result).hexdigest() == VOCAB_SHA256
    assert any(float(line.split("\t")[1]) != 0 for line in result.decode().splitlines())
    path.write_text(original.replace("▁П 152", "▁X 152") + "<blk> 1024\n")
    with pytest.raises(ValueError, match="не совпадают"):
        vocabulary(path)


def test_prepare_copies_authentic_vocabulary_without_overwriting_old_frozen_jobs(
    tmp_path, monkeypatch
):
    from involta_voice.config import Settings
    from involta_voice.sherpa import MODELS, model_path, prepare
    from involta_voice.sherpa_tokenizer import VOCAB_FILE

    config = Settings(
        _env_file=None,
        database_url="postgresql://test:test@localhost/test",
        service_secret="x" * 40,
        asr_models_dir=tmp_path,
    )
    root = model_path(config, "gigaam")
    root.mkdir(parents=True)
    (root / "bpe.vocab").write_bytes(b"legacy-zero-score-file")
    sp = tokenizer()

    def download(*args, local_dir, **kwargs):
        for file in MODELS["gigaam"]["files"]:
            (local_dir / file).write_text("fixture")
        (local_dir / "tokens.txt").write_text(
            "".join(f"{sp.id_to_piece(i)} {i}\n" for i in range(sp.vocab_size())) + "<blk> 1024\n"
        )

    monkeypatch.setattr("huggingface_hub.snapshot_download", download)
    prepare(config)
    assert (root / "bpe.vocab").read_bytes() == b"legacy-zero-score-file"
    assert hashlib.sha256((root / VOCAB_FILE).read_bytes()).hexdigest() == VOCAB_SHA256
    prepare(config)
    assert hashlib.sha256((root / VOCAB_FILE).read_bytes()).hexdigest() == VOCAB_SHA256
