/* Standalone voice widget: browser APIs only, no React or shared app runtime. */
(() => {
  for (const root of document.querySelectorAll("[data-voice-widget]")) {
    if (root.dataset.mounted) continue;
    root.dataset.mounted = "yes";
    const base = root.dataset.apiBase;
    const locationId = root.dataset.locationId;
    const controls = root.querySelector("[data-controls]");
    const error = root.querySelector("[data-error]");
    const history = root.querySelector("[data-history]");
    let recorder,
      stream,
      blob,
      preview,
      timer,
      seconds = 0,
      state = "idle",
      started = 0,
      polling = false;
    function showError(message = "") {
      error.textContent = message;
      error.hidden = !message;
    }
    function release() {
      stream?.getTracks().forEach((t) => t.stop());
      clearInterval(timer);
    }
    function clearPreview() {
      if (preview) URL.revokeObjectURL(preview);
      preview = null;
    }
    function button(text, handler, className = "") {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = text;
      b.className = className;
      b.addEventListener("click", handler);
      return b;
    }
    function render() {
      controls.replaceChildren();
      if (state === "idle")
        controls.append(button("● Записать отзыв", start, "record-button"));
      else if (state === "recording")
        controls.append(
          button(
            `■ Остановить · ${seconds} с`,
            stop,
            "record-button recording",
          ),
        );
      else {
        const audio = document.createElement("audio");
        audio.controls = true;
        audio.src = preview;
        controls.append(audio);
        const row = document.createElement("div");
        row.className = "button-row";
        const sendButton = button(
          state === "sending" ? "Отправляем…" : "Отправить ↗",
          send,
        );
        sendButton.disabled = state === "sending";
        row.append(sendButton);
        const again = button(
          "Записать заново",
          () => {
            blob = null;
            clearPreview();
            state = "idle";
            showError();
            render();
          },
          "secondary",
        );
        again.disabled = state === "sending";
        row.append(again);
        controls.append(row);
      }
    }
    async function load() {
      if (polling) return;
      polling = true;
      try {
        const response = await fetch(
          `${base}/my-reviews?location=${encodeURIComponent(locationId)}`,
          { credentials: "same-origin" },
        );
        if (!response.ok) return;
        const reviews = await response.json();
        history.replaceChildren();
        history.hidden = !reviews.length;
        if (!reviews.length) return;
        const heading = document.createElement("h3");
        heading.textContent = "Ваш голос в работе";
        history.append(heading);
        for (const r of reviews) {
          const row = document.createElement("div");
          row.className = "status-line";
          const date = document.createElement("span");
          date.textContent = new Date(r.created_at + "Z").toLocaleString(
            "ru-RU",
          );
          const status = document.createElement("strong");
          status.textContent = r.fraud
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
                        : "Получен · в очереди";
          row.append(date, status);
          history.append(row);
        }
        const note = document.createElement("small");
        note.textContent =
          "История доступна в этом браузере благодаря cookie. Здесь показан ваш статус; публичная подборка меняется только после публикации редактором.";
        history.append(note);
      } catch {
      } finally {
        polling = false;
      }
    }
    async function start() {
      showError();
      if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
        showError(
          "Запись недоступна. Откройте сайт по HTTPS в современном браузере.",
        );
        return;
      }
      try {
        controls.querySelector("button").disabled = true;
        stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        const mime = [
          "audio/webm;codecs=opus",
          "audio/mp4",
          "audio/ogg;codecs=opus",
        ].find((m) => MediaRecorder.isTypeSupported(m));
        recorder = new MediaRecorder(
          stream,
          mime ? { mimeType: mime } : undefined,
        );
        const chunks = [];
        let failed = false;
        recorder.ondataavailable = (e) => {
          if (e.data.size) chunks.push(e.data);
        };
        recorder.onstop = () => {
          release();
          if (failed) return;
          blob = new Blob(chunks, { type: recorder.mimeType });
          clearPreview();
          preview = URL.createObjectURL(blob);
          state = "ready";
          render();
        };
        recorder.onerror = () => {
          failed = true;
          release();
          state = "idle";
          render();
          showError("Запись прервана. Попробуйте ещё раз.");
        };
        seconds = 0;
        blob = null;
        started = performance.now();
        recorder.start(1000);
        state = "recording";
        render();
        timer = setInterval(() => {
          seconds = Math.floor((performance.now() - started) / 1000);
          if (seconds >= 120) stop();
          else
            controls.querySelector("button").textContent =
              `■ Остановить · ${seconds} с`;
        }, 250);
      } catch {
        release();
        state = "idle";
        render();
        showError(
          "Нет доступа к микрофону. Разрешите запись в настройках браузера.",
        );
      }
    }
    function stop() {
      seconds = Math.min(120, Math.floor((performance.now() - started) / 1000));
      if (recorder?.state === "recording") recorder.stop();
      release();
    }
    async function send() {
      if (!blob || state === "sending") return;
      state = "sending";
      showError();
      render();
      try {
        const form = new FormData();
        form.set("audio", blob, "voice");
        form.set("locationId", locationId);
        form.set("duration", String(seconds));
        if(root.dataset.csrf)form.set("_csrf",root.dataset.csrf);
        const response = await fetch(`${base}/reviews`, {
          method: "POST",
          body: form,
          credentials: "same-origin",
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "Не удалось отправить");
        blob = null;
        clearPreview();
        state = "idle";
        render();
        await load();
      } catch (e) {
        state = "ready";
        render();
        showError(e.message || "Не удалось отправить");
      }
    }
    root.querySelector("[data-start]").addEventListener("click", start);
    const poll = setInterval(load, 15000);
    load();
    window.addEventListener(
      "pagehide",
      () => {
        clearInterval(poll);
        release();
        clearPreview();
      },
      { once: true },
    );
  }
})();
