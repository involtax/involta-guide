"""The authentic GigaAM tokenizer, pinned to the IDs of our ONNX export."""

import hashlib
from functools import cache
from pathlib import Path
from typing import Any

ENCODING = "gigaam-sentencepiece-native-v2"
TOKENIZER_SHA256 = "828c12c991019eef952a960661f25a92d6ad279591e2ea466b4aeddf1d20a18a"
VOCAB_SHA256 = "af72e53d851552fca7510fd1aa8ca7db340407ee1c57bd34c4bafaec1a79529c"
VOCAB_FILE = "bpe.gigaam-native-v2.vocab"
TOKENIZER_FILE = Path(__file__).parent / "assets/gigaam-v3-e2e-rnnt.model"


@cache
def tokenizer() -> Any:
    import sentencepiece

    if hashlib.sha256(TOKENIZER_FILE.read_bytes()).hexdigest() != TOKENIZER_SHA256:
        raise ValueError("Не совпадает SHA256 родного токенизатора GigaAM")
    return sentencepiece.SentencePieceProcessor(model_file=str(TOKENIZER_FILE))


def vocabulary(tokens_file: Path) -> bytes:
    sp = tokenizer()
    tokens = {
        int(index): piece
        for piece, index in (line.rsplit(" ", 1) for line in tokens_file.read_text().splitlines())
    }
    if len(tokens) != sp.vocab_size() + 1 or tokens.get(sp.vocab_size()) != "<blk>":
        raise ValueError("Неверный размер словаря ONNX GigaAM")
    if any(tokens.get(i) != sp.id_to_piece(i) for i in range(sp.vocab_size())):
        raise ValueError("Токены родного GigaAM и ONNX-экспорта не совпадают")
    result = "".join(
        f"{sp.id_to_piece(i)}\t{sp.get_score(i)}\n" for i in range(sp.vocab_size())
    ).encode()
    if hashlib.sha256(result).hexdigest() != VOCAB_SHA256:
        raise ValueError("Не совпадает SHA256 весов токенизатора GigaAM")
    return result
