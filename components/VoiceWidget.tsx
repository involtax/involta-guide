"use client";
import { useEffect, useRef, useState } from "react";
type Status = {
  id: string;
  moderation: string;
  status: string;
  fraud: number;
  created_at: string;
  proposals: { status: string; kind: string }[];
  published: boolean;
};
export default function VoiceWidget({
  locationId,
  locationName,
}: {
  locationId: string;
  locationName: string;
}) {
  const [state, setState] = useState<
    "idle" | "recording" | "ready" | "sending"
  >("idle");
  const [error, setError] = useState("");
  const [blob, setBlob] = useState<Blob | null>(null);
  const [seconds, setSeconds] = useState(0);
  const [consent, setConsent] = useState(false);
  const [history, setHistory] = useState<Status[]>([]);
  const [preview, setPreview] = useState("");
  const recorder = useRef<MediaRecorder | null>(null);
  const stream = useRef<MediaStream | null>(null);
  const chunks = useRef<Blob[]>([]);
  async function load() {
    try {
      const r = await fetch(`/api/my-reviews?location=${locationId}`);
      if (r.ok) setHistory(await r.json());
    } catch {}
  }
  useEffect(() => {
    load();
    const timer = setInterval(load, 15000);
    return () => {
      clearInterval(timer);
      stream.current?.getTracks().forEach((t) => t.stop());
    };
  }, [locationId]);
  useEffect(() => {
    if (!blob) {
      setPreview("");
      return;
    }
    const url = URL.createObjectURL(blob);
    setPreview(url);
    return () => URL.revokeObjectURL(url);
  }, [blob]);
  useEffect(() => {
    if (state !== "recording") return;
    const timer = setInterval(() => setSeconds((s) => s + 1), 1000);
    return () => clearInterval(timer);
  }, [state]);
  useEffect(() => {
    if (seconds >= 120 && state === "recording") stop();
  }, [seconds, state]);
  function stop() {
    if (recorder.current?.state === "recording") recorder.current.stop();
    stream.current?.getTracks().forEach((t) => t.stop());
  }
  async function start() {
    setError("");
    if (!consent) {
      setError("Подтвердите согласие на обработку записи.");
      return;
    }
    if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
      setError(
        "Запись недоступна. Откройте сайт по HTTPS в современном браузере.",
      );
      return;
    }
    try {
      stream.current = await navigator.mediaDevices.getUserMedia({
        audio: true,
      });
      const mime = [
        "audio/webm;codecs=opus",
        "audio/mp4",
        "audio/ogg;codecs=opus",
      ].find((m) => MediaRecorder.isTypeSupported(m));
      const r = new MediaRecorder(
        stream.current,
        mime ? { mimeType: mime } : undefined,
      );
      chunks.current = [];
      r.ondataavailable = (e) => {
        if (e.data.size) chunks.current.push(e.data);
      };
      r.onstop = () => {
        setBlob(new Blob(chunks.current, { type: r.mimeType }));
        setState("ready");
      };
      r.onerror = () => {
        setError("Запись прервана. Попробуйте ещё раз.");
        stop();
      };
      recorder.current = r;
      setSeconds(0);
      setBlob(null);
      r.start(1000);
      setState("recording");
    } catch {
      stream.current?.getTracks().forEach((t) => t.stop());
      setError(
        "Нет доступа к микрофону. Разрешите запись в настройках браузера.",
      );
    }
  }
  async function send() {
    if (!blob) return;
    setState("sending");
    setError("");
    try {
      const f = new FormData();
      f.set("audio", blob, "voice");
      f.set("locationId", locationId);
      f.set("duration", String(seconds));
      f.set("consent", "yes");
      const r = await fetch("/api/reviews", { method: "POST", body: f });
      const data = await r.json();
      if (!r.ok) throw new Error(data.error);
      setBlob(null);
      setState("idle");
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Не удалось отправить");
      setState("ready");
    }
  }
  return (
    <section className="voice-section" id="review">
      <div>
        <p className="eyebrow">ВАШ ГОЛОС В ГИДЕ</p>
        <h2>
          Были здесь?
          <br />
          <span className="serif">Расскажите.</span>
        </h2>
        <p>
          Хороший ужин, неудачный опыт или место, которого не хватает в подборке{" "}
          {locationName}? Нажмите кнопку и расскажите голосом. Редакция услышит.
        </p>
        <small>
          До 2 минут. Без регистрации. Отзыв появится в обработке, а изменения в
          гиде проверит редактор.
        </small>
      </div>
      <div className="recorder">
        <label className="consent">
          <input
            type="checkbox"
            checked={consent}
            onChange={(e) => setConsent(e.target.checked)}
            disabled={state !== "idle"}
          />{" "}
          Согласен на хранение аудио и обработку отзыва, включая расшифровку
          через OpenAI. Не сообщайте личные данные других людей.
        </label>
        {state === "recording" ? (
          <button className="record-button recording" onClick={stop}>
            <span>■</span>Остановить · {seconds} с
          </button>
        ) : state === "idle" ? (
          <button className="record-button" onClick={start}>
            <span>●</span>Записать отзыв
          </button>
        ) : (
          <>
            <audio controls src={preview} />
            <div className="button-row">
              <button onClick={send} disabled={state === "sending"}>
                {state === "sending" ? "Отправляем…" : "Отправить ↗"}
              </button>
              <button
                className="secondary"
                disabled={state === "sending"}
                onClick={() => {
                  setBlob(null);
                  setState("idle");
                }}
              >
                Записать заново
              </button>
            </div>
          </>
        )}
        {error && (
          <p role="alert" className="error">
            {error}
          </p>
        )}
      </div>
      {history.length > 0 && (
        <div className="my-reviews">
          <h3>Ваш голос в работе</h3>
          {history.map((r) => (
            <div className="status-line" key={r.id}>
              <span>
                {new Date(r.created_at + "Z").toLocaleString("ru-RU")}
              </span>
              <strong>
                {r.fraud
                  ? "Отзыв не учтён"
                  : r.moderation === "rejected"
                    ? "Редакция не одобрила"
                    : r.published
                      ? "На основе отзыва опубликованы изменения"
                      : r.proposals.some((p) => p.status === "approved")
                        ? "Изменения добавлены в черновик"
                        : r.moderation === "approved"
                          ? "Отзыв принят редакцией"
                          : r.status === "processed"
                            ? "Разобран · ожидает редактора"
                            : r.status === "failed"
                              ? "Нужна повторная обработка"
                              : "Получен · в очереди"}
              </strong>
            </div>
          ))}
          <small>
            История доступна в этом браузере благодаря cookie. Здесь показан ваш
            статус; публичная подборка меняется только после публикации
            редактором.
          </small>
        </div>
      )}
    </section>
  );
}
