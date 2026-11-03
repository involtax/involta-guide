/* Standalone v1 embed: no Guide DOM, styles, cookies or implementation imports. */
(() => {
  const script = document.currentScript;
  if (!window.InvoltaVoice) {
    const instances = new Map();
    const allowed = {
      idle: ["permission_pending"], permission_pending: ["recording", "error"],
      recording: ["preview", "error"], preview: ["permission_pending", "uploading"],
      uploading: ["accepted", "error"], accepted: ["permission_pending"],
      error: ["permission_pending", "uploading", "preview"],
    };
    class Machine {
      state = "idle";
      move(next) {
        if (!allowed[this.state].includes(next)) throw new Error("Invalid recording transition");
        this.state = next;
      }
    }
    function mount(target, options = {}) {
      if (typeof target === "string") target = document.getElementById(target);
      if (!target || instances.has(target)) return instances.get(target);
      const root = target.shadowRoot || target.attachShadow({ mode: "open" });
      const api = new URL(options.apiBase || "/guide/voice/api/v1", location.href);
      if (api.origin !== location.origin) throw new Error("Voice gateway must share page origin");
      const machine = new Machine();
      let recorder, stream, timer, poll, historyPoll, previewUrl, blob, key, reference;
      let chunks = [], disposed = false, started = 0, delay = 2000, bytes = 0;
      const style = document.createElement("style");
      style.textContent = ":host{display:block;font:16px/1.5 system-ui;color:inherit}section{padding:24px;border:1px solid #ddd;border-radius:12px}h2{font-size:24px;margin:0 0 12px}button{font:inherit;padding:12px 18px;margin:6px 8px 6px 0;cursor:pointer;border:1px solid currentColor;border-radius:6px;background:transparent;color:inherit}button:disabled{opacity:.5;cursor:wait}audio{display:block;max-width:100%;margin:12px 0}.error{color:#a21d1d}.history{white-space:pre-wrap}small{display:block}button:focus-visible{outline:3px solid #5577cc}@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}";
      const panel = document.createElement("section");
      panel.setAttribute("aria-label", "Голосовой отзыв");
      const heading = document.createElement("h2"); heading.textContent = "Расскажите о своём опыте";
      const description = document.createElement("p"); description.textContent = "Запишите отзыв до двух минут. Можно прослушать запись перед отправкой.";
      const controls = document.createElement("div");
      const status = document.createElement("p"); status.setAttribute("role", "status"); status.setAttribute("aria-live", "polite");
      const error = document.createElement("p"); error.className = "error"; error.setAttribute("role", "alert");
      const history = document.createElement("div"); history.className = "history";
      const currentText = document.createElement("p");
      panel.append(heading, description, controls, status, error, currentText, history);
      root.replaceChildren(style, panel);
      function button(label, action, disabled = false) {
        const b = document.createElement("button"); b.type = "button"; b.textContent = label;
        b.disabled = disabled; b.addEventListener("click", action); return b;
      }
      function release() {
        clearInterval(timer); timer = null;
        stream?.getTracks().forEach(track => track.stop()); stream = null;
      }
      function clearBlob() {
        if (previewUrl) URL.revokeObjectURL(previewUrl);
        previewUrl = null; blob = null; key = null; chunks = []; bytes = 0;
      }
      function render() {
        if (disposed) return;
        target.dataset.voiceState = machine.state;
        controls.replaceChildren();
        if (machine.state === "recording") {
          controls.append(button("■ Остановить запись", stop));
        } else if (["permission_pending", "uploading"].includes(machine.state)) {
          controls.append(button(machine.state === "uploading" ? "Отправляем…" : "Ждём разрешения микрофона…", () => {}, true));
        } else {
          if (blob && ["preview", "error"].includes(machine.state)) {
            if (!previewUrl) previewUrl = URL.createObjectURL(blob);
            const player = document.createElement("audio"); player.controls = true; player.src = previewUrl;
            player.setAttribute("aria-label", "Прослушать запись");
            controls.append(player, button("Отправить отзыв", send));
          }
          controls.append(button(blob ? "Записать заново" : "● Записать отзыв", start));
        }
      }
      function transition(next) { machine.move(next); render(); }
      function fail(message) {
        error.textContent = message;
        if (machine.state !== "error") transition("error"); else render();
      }
      async function start() {
        error.textContent = ""; status.textContent = ""; transition("permission_pending");
        if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
          fail("Запись недоступна. Нужен современный браузер и HTTPS."); return;
        }
        try {
          const incoming = await navigator.mediaDevices.getUserMedia({ audio: true });
          if (disposed) { incoming.getTracks().forEach(t => t.stop()); return; }
          stream = incoming; clearBlob();
          const mime = ["audio/webm;codecs=opus", "audio/mp4", "audio/ogg;codecs=opus"].find(t => MediaRecorder.isTypeSupported(t));
          recorder = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
          recorder.ondataavailable = event => {
            if (disposed || !event.data.size) return;
            chunks.push(event.data); bytes += event.data.size;
            if (bytes > 10 * 1024 * 1024) {
              recorder.onstop = () => {}; if (recorder.state !== "inactive") recorder.stop();
              release(); clearBlob(); fail("Запись больше 10 МБ. Запишите более короткий отзыв.");
            }
          };
          recorder.onerror = () => { release(); fail("Не удалось записать звук. Попробуйте ещё раз."); };
          recorder.onstop = () => {
            release(); if (disposed || machine.state !== "recording") return;
            blob = new Blob(chunks, { type: recorder.mimeType || "audio/webm" });
            key = crypto.randomUUID(); status.textContent = "Запись готова к прослушиванию."; transition("preview");
          };
          recorder.start(250); started = performance.now(); transition("recording");
          timer = setInterval(() => {
            const seconds = Math.floor((performance.now() - started) / 1000);
            status.textContent = `Запись: ${seconds} с из 120`;
            if (seconds >= 120) stop();
          }, 500);
        } catch {
          release(); fail("Нет доступа к микрофону. Разрешите запись в настройках браузера.");
        }
      }
      function stop() { if (recorder?.state === "recording") recorder.stop(); }
      async function send() {
        if (!blob || !key) return;
        error.textContent = ""; transition("uploading");
        const form = new FormData();
        form.set("audio", blob, "recording"); form.set("location", options.location || "");
        form.set("idempotency_key", key); form.set("language", navigator.language || "");
        form.set("timezone", Intl.DateTimeFormat().resolvedOptions().timeZone || "");
        try {
          // Establish ownership before upload: losing the later 202 must not lose the visitor cookie.
          const identity = await fetch(api.href + "/identity", { method: "POST", credentials: "same-origin" });
          if (!identity.ok) throw new Error("Не удалось подготовить отправку. Попробуйте ещё раз.");
          const response = await fetch(api.href + "/submissions", { method: "POST", credentials: "same-origin", body: form });
          const data = await response.json();
          if (!response.ok) throw new Error(data.error?.message || "Не удалось отправить запись.");
          if (disposed) return;
          reference = data.reference; clearBlob(); transition("accepted");
          status.textContent = "Запись доставлена. Расшифровываем ваш отзыв."; delay = 2000;
          schedule();
        } catch (e) {
          if (!disposed) fail(e.message || "Связь прервалась. Повторите отправку этой записи.");
        }
      }
      const labels = { pending: "Ожидает редактора", approved: "Принято", rejected: "Отклонено", fraud: "Помечено как фрод", published: "Опубликовано" };
      function schedule() { clearTimeout(poll); if (!disposed) poll = setTimeout(pollStatus, delay); }
      async function pollStatus() {
        if (disposed) return;
        if (document.hidden) { schedule(); return; }
        try {
          const response = await fetch(api.href + "/submissions/" + encodeURIComponent(reference), { credentials: "same-origin" });
          if (!response.ok) throw new Error("status");
          const data = await response.json();
          if (disposed) return;
          status.textContent = data.transcript ? "Текст отзыва сохранён. " + (labels[data.editorial_state || data.moderation] || "Ожидает редактора") : "Запись доставлена. Обрабатываем.";
          currentText.textContent = data.transcript || "";
          if (data.state.includes("failed") || data.state.includes("unknown")) {
            status.textContent = data.transcript ? "Текст отзыва сохранён и доступен редактору. Дополнительный анализ не завершён." : "Запись сохранена. Расшифровка не завершена; редактор может проверить ошибку.";
            return;
          }
          if (["approved", "published", "rejected", "fraud"].includes(data.editorial_state || data.moderation)) return;
          delay = Math.min(delay * 1.5, 60000); schedule();
        } catch { delay = Math.min(delay * 2, 60000); schedule(); }
      }
      async function loadHistory() {
        if (disposed) return;
        if (document.hidden) { historyPoll = setTimeout(loadHistory, 15000); return; }
        let ongoing = false;
        try {
          const response = await fetch(api.href + "/submissions/mine?location=" + encodeURIComponent(options.location || ""), { credentials: "same-origin" });
          if (!response.ok) return;
          const data = await response.json();
          if (disposed) return;
          history.replaceChildren();
          if (data.rows?.length) {
            const title = document.createElement("h3"); title.textContent = "Ваши отзывы"; history.append(title);
          }
          for (const item of data.rows || []) {
            const paragraph = document.createElement("p");
            const state = item.editorial_state || item.moderation;
            paragraph.textContent = (labels[state] || "Обрабатывается") + (item.transcript ? ": " + item.transcript : "");
            history.append(paragraph);
            ongoing ||= !["approved", "published", "rejected", "fraud"].includes(state);
          }
        } catch { ongoing = true; }
        if (ongoing && !disposed) historyPoll = setTimeout(loadHistory, 15000);
      }
      function unmount() {
        disposed = true; clearTimeout(poll); clearTimeout(historyPoll); if (recorder?.state === "recording") recorder.stop();
        release(); clearBlob(); root.replaceChildren(); instances.delete(target);
      }
      const instance = { unmount, get state() { return machine.state; } };
      instances.set(target, instance); render(); loadHistory(); return instance;
    }
    window.InvoltaVoice = { mount, unmount(target) { instances.get(typeof target === "string" ? document.getElementById(target) : target)?.unmount(); }, Machine };
  }
  if (script?.dataset.target) window.InvoltaVoice.mount(script.dataset.target, { apiBase: script.dataset.apiBase, location: script.dataset.location });
})();
