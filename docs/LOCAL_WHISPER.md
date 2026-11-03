# Local Russian speech recognition

Audio transcription runs on the application server with faster-whisper /
CTranslate2. The OpenAI audio endpoint is removed from both visitor Voice and
trusted Guide/admin execution. OpenAI remains optional for analysis of the
already saved text. Missing key/API budget cannot block local transcription.
No extra speech database is needed: binaries + pinned model weights are enough.

## Models and reproducibility

- `large-v3-russian` (default): bzikst/faster-whisper-large-v3-russian-int8,
  revision 157ceddec5214ab3f5f710ae8e377058807d2e97. Conversion of
  antony66/whisper-large-v3-russian, fine-tuned on Common Voice 17 Russian.
- `large-v3`: Systran/faster-whisper-large-v3,
  revision edaa852ec7e145841d8ffdb056a99866b5f0a478.

The Russian model author's reported WER (6.39 versus 9.84 for the base model)
is from their own split/evaluation, not a guarantee on our recordings. Benchmark
real voice, noise, names and numbers before selecting the production model.
Model revisions, decoder settings and device/precision are snapshotted per run.
Only pinned local files are used in serving; runtime never downloads weights or
falls back to a cloud transcription service.

## Install and prepare

```bash
uv sync --frozen --extra voice-local
uv run voice prepare-model
uv run voice-worker
```

`VOICE_TRANSCRIPTION_MODEL=large-v3-russian`, `VOICE_WHISPER_MODELS_DIR`,
`VOICE_WHISPER_DEVICE=cpu`, `VOICE_WHISPER_COMPUTE_TYPE=int8`,
`VOICE_WHISPER_CPU_THREADS=4`. Guide's trusted worker uses the same VOICE_ names.
Defaults target CPU. CUDA requires an appropriate NVIDIA runtime/CUDA libraries;
that deployment profile is not supplied or assumed here.

Production/local Compose prepare weights into `voice-models` once and mount them
read-only into the serving workers. Prepare finishes before real Voice worker
starts; its startup warms the model before emitting a healthy heartbeat. The
Guide admin worker reads the same volume for trusted speech. Budget RAM for both
processes if used simultaneously. API/web do not initialize speech models.
Docker image includes the locked optional dependency group and libgomp1.

## Processing and failure behavior

Russian language, transcription (not translation), beam search, silence/VAD
filtering and disabled previous-text conditioning are explicit. A silent file
produces a clear error rather than an invented review. Raw local text/segments
are persisted before downstream analysis. Local calls have zero API reservation
and API cost; server CPU/RAM/storage still have their normal operational costs.

A heartbeat renews long CPU inference leases; only one Voice transcription runs
at a time. Lost local attempts may retry up to three claims without a paid POST.
Expired unknown analysis POSTs retain the existing no-blind-retry policy. A saved
transcript is reused by new analysis runs. Existing audio, transcripts and raw
responses are retained; starting a fresh run normalizes obsolete cloud model
settings to a local model. Saved completed responses remain revalidatable.

The trusted admin command executor and permissions stay in Guide, with local
speech recognition and private audit. No default API key is required for speech;
it is only required for optional text analysis/privileged command interpretation.
