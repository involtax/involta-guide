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
`VOICE_WHISPER_CPU_THREADS=2`. Guide's trusted worker uses the same VOICE_ names.
Defaults target CPU. CUDA requires an appropriate NVIDIA runtime/CUDA libraries;
that deployment profile is not supplied or assumed here.

Production/local Compose prepare weights into `voice-models` once and mount them
read-only into the serving workers. Prepare finishes before real Voice worker
starts; its startup warms the model before emitting a healthy heartbeat. The Guide admin executor queues transcription-only tasks to this same Voice worker
using an authenticated internal endpoint. These tasks do not create a second
visitor review or emit a moderation event. API/web/Guide never load a second
Whisper model. Only one inference is claimed at a time.
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

## Production hardware observed (read-only inventory)

2 vCPU, 3,900,652 KiB total RAM, 3,039,992 KiB available at inspection,
no NVIDIA device reported. Do not start separate model instances for admin and
visitors. CPU int8 is the initial target; queue latency and RAM headroom require
measurement on that server before any rollout. No production changes were made.

The CPU production worker is limited to 3 GiB / 1.5 CPUs so inference cannot
consume the entire 4 GiB server. The base large-v3 comparison used more memory
than that worker limit; selecting it requires provisioning a larger worker.
A cgroup preflight refuses insufficient model memory before allocating weights.
When model/settings change, the old model is released before loading a new one.

## Measured checks (2026-11-03)

- Full regression before the two shared-Guide cases: 256 passed. Shared Guide /
  private STT cases plus local decoder unit cases: 8 passed.
- Real model: Russian synthetic speech generated offline with eSpeak NG;
  9.1205 seconds. This is a decoder/inference smoke, not a human-voice WER set.
- Network-isolated read-only Docker container, 1.5 CPUs, 3 GiB limit:
  Russian int8 checkpoint warmup 4.78s, inference 31.10s, peak RSS 2312 MiB.
  Two seconds of silent WAV correctly returned “Речь не обнаружена”.
- Base large-v3 comparison on the same execution machine (4 threads, outside the
  constrained container): warmup 8.55s, inference 18.85s, peak RSS 3092 MiB.
- Russian tuned model on 4 threads: warmup 1.71s, inference 16.29s, 2302 MiB RSS.
  Proper names were wrong on synthetic speech; context hotwords helped “Огонёк”
  but did not fix every name. No automatic text substitution is performed.
- PyAV 19.0.2 broke faster-whisper 1.2.1 `metadata_errors` argument. The dependency
  group now requires av>=14,<16; lock pins PyAV 15.1.0, verified with real decoding.

The current server is small for this model. Long messages will queue, and its
CPU/real-world latency may differ. A larger CPU/RAM allocation or suitable GPU
is needed if this speed is unacceptable. Model quality still needs real Russian
microphone recordings (names, numbers, noise); this implementation is not claimed
to have passed that acceptance. No production rollout is part of this branch.

## Private Guide speech queue

POST `/internal/v1/guide-transcriptions` uses the existing internal service
credential and SubmissionMetadata. Purpose is set by the server, not a browser
field. These tasks persist audio and local speech logs, never enqueue analysis,
never emit review outbox events and are excluded from review history/queues.
Guide polls the owner-checked internal status and keeps the original review,
admin-session identity, permissions and command interpreter in Guide. Retry of
an accepted request returns the same reference/transcript. Private task audio is
kept under the same no-automatic-expiry policy. Voice migration 002 adds purpose
without changing existing review IDs or deleting data.
