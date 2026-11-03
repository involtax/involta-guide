import hashlib
import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from involta_voice.beta import summary
from involta_voice.errors import VoiceError
from involta_voice.evaluation import Evaluation, evaluate, queue_evaluation, report
from involta_voice.main import create_app
from involta_voice.provider import FakeProvider, ProviderFailure
from involta_voice.service import VoiceService
from involta_voice.worker import Worker

from .test_voice_service import metadata, recording
from .test_voice_service import voice_database as voice_database


class Pair(FakeProvider):
    def __init__(self, failed=None):
        self.failed = failed
        self.calls = []

    def call(self, kind, request, audio, mime):
        self.calls.append((kind, request["model"]))
        if request["model"] == self.failed:
            raise ProviderFailure("local model failed")
        result = super().call(kind, request, audio, mime)
        if kind == "transcription":
            result.update(
                request_id=None,
                body=json.dumps(
                    {
                        "text": "В Огоньке вкусно."
                        if request["model"] == "gigaam"
                        else "В огне вкусно.",
                        "elapsed_seconds": 1.0,
                        "pcm_sha256": "same-decoded-input",
                    }
                ),
            )
        return result


def beta(voice_database):
    settings, engine = voice_database
    # Explicit injected adapter, no external API. Mark synthetic candidates as non-fake
    # only in these tests to exercise the real-data aggregate query.
    settings = settings.model_copy(update={"asr_beta_enabled": True, "fake_provider": False})
    return settings, engine, VoiceService(engine, settings)


@pytest.mark.parametrize("failed", [None, "gigaam", "parakeet"])
def test_two_models_save_one_review_with_independent_failure(voice_database, failed):
    settings, engine, service = beta(voice_database)
    ref = service.submit(metadata(), recording(), "audio/wav")["reference"]
    provider = Pair(failed)
    worker = Worker(engine, settings, provider)
    assert worker.once()
    if failed == "gigaam":
        assert service.status(ref)["transcript"] is None
        assert service.status(ref)["state"] not in ("transcription_failed", "transcription_unknown")
        assert worker.once()  # fallback commits the only transcript event
    expected = "В огне вкусно." if failed == "gigaam" else "В Огоньке вкусно."
    assert service.status(ref)["transcript"] == expected
    while worker.once():
        pass
    assert service.status(ref)["transcript"] == expected
    assert service.status(ref)["state"] == "result_ready"
    trial = report(engine, ref)["runs"][0]
    assert trial["source_engine"] == ("parakeet" if failed == "gigaam" else "gigaam")
    assert {c["engine"] for c in trial["candidates"]} == {"gigaam", "parakeet"}
    assert sum(c["state"] == "failed" for c in trial["candidates"]) == bool(failed)
    with engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM outbox WHERE kind='transcript_ready'")
            ).scalar_one()
            == 1
        )
        assert (
            conn.execute(
                text("SELECT count(*) FROM outbox WHERE kind='processing_failed'")
            ).scalar_one()
            == 0
        )
        candidates = conn.execute(text("SELECT * FROM asr_candidates")).mappings().all()
        assert all(
            c["details"]["audio_sha256"] == hashlib.sha256(recording()).hexdigest()
            for c in candidates
        )
        assert all(c["details"]["pcm_sha256"] == "same-decoded-input" for c in candidates)
    assert len([c for c in provider.calls if c[0] == "analysis"]) == 1


def test_saved_secondary_raw_recovers_without_provider_and_fake_is_excluded(voice_database):
    settings, engine = voice_database
    settings = settings.model_copy(update={"asr_beta_enabled": True})
    service = VoiceService(engine, settings)
    ref = service.submit(metadata(), recording(), "audio/wav")["reference"]
    worker = Worker(engine, settings, FakeProvider())
    assert worker.once()
    stage = worker.claim()
    assert stage["kind"] == "analysis"
    worker.process(stage)
    stage = worker.claim()
    assert stage["kind"] == "comparison"
    raw = {"status_code": 200, "request_id": "fake-saved", "body": '{"text":"Другой текст."}'}
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE stages SET raw_response=CAST(:raw AS jsonb),lease_until=now()-interval '1 second' WHERE kind='comparison'"
            ),
            {"raw": json.dumps(raw)},
        )

    class NoCalls:
        def call(self, *args):
            raise AssertionError("reuse saved response")

    assert Worker(engine, settings, NoCalls()).once()
    assert all(c["state"] == "completed" for c in report(engine, ref)["runs"][0]["candidates"])
    assert summary(engine, settings)["recordings"] == 0
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM outbox")).scalar_one() == 2


def test_beta_internal_api_requires_auth_session_and_strict_commands(voice_database):
    settings, engine, service = beta(voice_database)
    ref = service.submit(metadata(), recording(), "audio/wav")["reference"]
    path = "/internal/v1/admin/submissions/" + ref
    with TestClient(create_app(settings)) as client:
        assert client.get(path + "/asr").status_code == 401
        headers = {"authorization": "Bearer " + settings.service_secret.get_secret_value()}
        assert (
            client.post(
                path + "/asr-runs",
                headers=headers,
                json={"idempotency_key": str(uuid4()), "force": "false"},
            ).status_code
            == 400
        )
        result = client.post(
            path + "/asr-runs", headers=headers, json={"idempotency_key": str(uuid4())}
        )
        assert result.status_code == 202 and result.json()["reused"]
        trial = client.get(path + "/asr", headers=headers).json()["runs"][0]
        body = {
            "idempotency_key": str(uuid4()),
            "revision": 0,
            "candidates_digest": trial["digest"],
        }
        assert (
            client.post(path + "/asr-runs/1/evaluation", headers=headers, json=body).status_code
            == 403
        )
        headers["x-guide-session"] = str(uuid4())
        assert (
            client.post(path + "/asr-runs/1/evaluation", headers=headers, json=body).status_code
            == 200
        )


def test_unimported_failed_recording_exposes_both_errors_and_can_run_comparison(
    database, voice_database
):
    from involta_guide.main import create_app as guide_app

    from .test_voice_guide import client_for
    from .test_web import login, token

    settings, engine = database
    vs, ve, service = beta(voice_database)
    settings = settings.model_copy(update={"voice_service_secret": vs.service_secret})
    ref = service.submit(metadata(), recording(), "audio/wav")["reference"]

    class BothFail(Pair):
        def call(self, *args):
            raise ProviderFailure("нет речи / ASR error")

    worker = Worker(ve, vs, BothFail())
    assert worker.once() and worker.once()
    app = guide_app(settings)
    app.state.voice_client = client_for(settings, vs)
    path = "/guide/admin/voice/submission/" + ref
    with TestClient(app) as client:
        login(client)
        page = client.get(path)
        assert page.status_code == 200 and "GigaAM" in page.text and "Parakeet" in page.text
        assert "ASR error" in page.text
        response = client.post(
            path,
            data={
                "_csrf": token(page.text),
                "action": "asr-run",
                "idempotency_key": str(uuid4()),
                "force": "yes",
            },
            headers={"origin": "http://testserver"},
        )
        assert response.status_code == 200, response.text
    with ve.connect() as conn:
        assert conn.execute(text("SELECT active_run FROM submissions")).scalar_one() == 1
        assert conn.execute(text("SELECT count(*) FROM outbox")).scalar_one() == 0


def test_normal_review_comparison_and_human_vote_never_modify_original_or_emit_extra_events(
    voice_database,
):
    settings, engine, service = beta(voice_database)
    ref = service.submit(metadata(), recording(), "audio/wav")["reference"]
    initial_worker = Worker(engine, settings, Pair())
    while initial_worker.once():
        pass
    original = service.status(ref)["transcript"]
    key = str(uuid4())
    queued = queue_evaluation(engine, settings, ref, key, force=True)
    assert queued["run"] == 2
    assert queue_evaluation(engine, settings, ref, key, force=True)["run"] == 2
    assert queue_evaluation(engine, settings, ref, str(uuid4()))["reused"]
    with pytest.raises(VoiceError, match="Ключ"):
        queue_evaluation(engine, settings, ref, key, force=False)
    provider = Pair()
    worker = Worker(engine, settings, provider)
    assert worker.once() and worker.once() and not worker.once()
    assert all(c[0] == "transcription" for c in provider.calls)
    trial = report(engine, ref)["runs"][0]
    command = Evaluation(
        idempotency_key=uuid4(),
        revision=0,
        candidates_digest=trial["digest"],
        preferred_engine="gigaam",
        reference_text="В Огоньке вкусно.",
        note="слушал оригинал",
    )
    session = str(uuid4())
    accepted = evaluate(engine, ref, 2, command, session)
    assert evaluate(engine, ref, 2, command, session) == accepted
    with pytest.raises(VoiceError, match="изменилась"):
        evaluate(engine, ref, 2, command.model_copy(update={"idempotency_key": uuid4()}), session)
    with pytest.raises(VoiceError, match="изменились"):
        evaluate(
            engine,
            ref,
            2,
            command.model_copy(
                update={"idempotency_key": uuid4(), "revision": 1, "candidates_digest": "0" * 64}
            ),
            session,
        )
    result = report(engine, ref)["runs"][0]
    assert next(c for c in result["candidates"] if c["engine"] == "gigaam")["wer"] == 0
    totals = summary(engine, settings)
    assert totals["recordings"] == totals["annotated_recordings"] == 1
    assert totals["engines"]["gigaam"]["votes"] == 1
    assert totals["engines"]["parakeet"]["wer"] > 0
    assert service.status(ref)["transcript"] == original
    with engine.connect() as conn:
        assert conn.execute(text("SELECT active_run FROM submissions")).scalar_one() == 1
        assert conn.execute(text("SELECT count(*) FROM outbox")).scalar_one() == 2
        event = conn.execute(text("SELECT * FROM asr_evaluation_events")).mappings().one()
        assert (
            str(event["session_id"]) == session
            and event["after_data"]["candidates_digest"] == trial["digest"]
        )
    assert queue_evaluation(engine, settings, ref, str(uuid4()), force=True)["run"] == 3
