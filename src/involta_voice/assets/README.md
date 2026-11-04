# GigaAM tokenizer

`gigaam-v3-e2e-rnnt.model` is the original GigaAM v3 E2E RNN-T tokenizer (MIT).
Source: https://cdn.chatwm.opensmodel.sberdevices.ru/GigaAM/v3_e2e_rnnt_tokenizer.model
Upstream loader: https://github.com/salute-developers/GigaAM/blob/main/gigaam/__init__.py
SHA256: 828c12c991019eef952a960661f25a92d6ad279591e2ea466b4aeddf1d20a18a
MD5: 3b3bf8370e882885d79731592fc99f98 (also recorded by Sherpa's GigaAM export script).

All 1024 pieces/IDs are checked against the pinned Sherpa GigaAM ONNX tokens.txt
before preparing a vocabulary. Preserve the authentic piece scores: assigning
zero to every piece causes Sherpa's encoder to prefer character segmentation.
The generated UTF-8 vocabulary SHA256 is pinned in sherpa_tokenizer.py.
No user audio or transcripts are included in this directory.
