/* One standalone recorder. All VAD assets are pinned and served from this origin. */
(() => {
  const script = document.currentScript;
  if (!window.InvoltaVoice) {
    const instances = new Map();
    const assetBase = new URL("vad/", script?.src || location.href).href;
    let activeCapture = null,
      runtimePromise;
    const releasedVads = new WeakSet();
    async function destroyVad(vad) {
      if (!vad || releasedVads.has(vad)) return;
      releasedVads.add(vad);
      try {
        await vad.destroy();
      } catch {
        // vad-web 0.0.30 destroy assumes graph initialization; release a cancelled model too.
        try {
          await vad.model?.release();
        } catch {
          /* already released by the library */
        }
      }
    }
    const MIN_MS = 5000,
      MAX_MS = 120000,
      MAX_BYTES = 10 * 1024 * 1024;
    const REQUEST =
      "твой голос важен. скажи что ты думаешь и о каком ресторане";
    const TOO_SHORT =
      "Слишком коротко. Запиши хотя бы 5 секунд: назови ресторан и расскажи, что думаешь.";
    const allowed = {
      idle: ["starting", "preview"],
      starting: ["recording", "stopping", "idle", "error", "preview"],
      recording: ["stopping", "idle", "error"],
      stopping: ["preview", "idle", "error"],
      preview: ["starting", "uploading", "idle"],
      uploading: ["accepted", "retry"],
      accepted: ["starting"],
      retry: ["starting", "uploading", "idle"],
      error: ["starting", "idle", "preview"],
    };
    class Machine {
      state = "idle";
      move(next) {
        if (!allowed[this.state].includes(next))
          throw new Error("Invalid recording transition");
        this.state = next;
      }
    }
    // Counts only processed, timely live frames. RMS never decides whether speech occurred.
    class SilenceGate {
      constructor() {
        this.last = null;
        this.processed = 0;
        this.positive = 0;
        this.silence = 0;
        this.spoken = false;
        this.disabled = false;
      }
      frame(probability, timestamp, frameMs = 32) {
        if (this.disabled) return {};
        if (
          !Number.isFinite(probability) ||
          !Number.isFinite(timestamp) ||
          !(frameMs > 0)
        ) {
          this.disabled = true;
          return { unavailable: true };
        }
        if (
          this.last !== null &&
          (timestamp - this.last > 250 || timestamp < this.last)
        ) {
          this.disabled = true;
          return { unavailable: true };
        }
        const elapsed =
          this.last === null
            ? frameMs
            : Math.min(frameMs, Math.max(0, timestamp - this.last));
        this.last = timestamp;
        this.processed += elapsed;
        let speech = false;
        if (probability >= 0.3) {
          this.positive += elapsed;
          this.silence = 0;
          if (!this.spoken && this.positive >= 400) {
            this.spoken = true;
            speech = true;
          }
        } else {
          this.positive = 0;
          this.silence =
            this.spoken && probability <= 0.25 ? this.silence + elapsed : 0;
        }
        return {
          speech,
          warning: this.spoken && this.silence >= 4500,
          stop:
            this.spoken && this.silence >= 5000
              ? "silence"
              : !this.spoken && this.processed >= 8000
                ? "no_speech"
                : null,
        };
      }
    }
    function loadScript(url) {
      return new Promise((resolve, reject) => {
        const node = document.createElement("script");
        node.src = url;
        node.onload = resolve;
        node.onerror = () => {
          node.remove();
          reject(new Error("VAD asset unavailable: " + url));
        };
        document.head.append(node);
      });
    }
    function loadRuntime() {
      return (runtimePromise ||= loadScript(assetBase + "ort.wasm.min.js")
        .then(() => loadScript(assetBase + "bundle.min.js"))
        .catch((e) => {
          runtimePromise = null;
          throw e;
        }));
    }
    class Diagnostics {
      constructor(root, { key, api, role = "visitor", location = "" }) {
        this.key = `${key}:${role}:${location}`;
        this.api = api;
        this.role = role;
        this.report = {
          version: 1,
          browser: navigator.userAgent,
          role,
          location,
          events: [],
        };
        this.panel = document.createElement("details");
        this.panel.open = false;
        const title = document.createElement("summary");
        title.textContent = "Диагностика записи · бета";
        this.info = document.createElement("pre");
        this.info.style.cssText =
          "white-space:pre-wrap;overflow-wrap:anywhere;max-height:32em;overflow:auto";
        this.raw = document.createElement("pre");
        this.raw.style.cssText = this.info.style.cssText;
        this.download = document.createElement("a");
        this.download.textContent = "Скачать аудио";
        this.download.hidden = true;
        const copy = document.createElement("button");
        copy.type = "button";
        copy.textContent = "Скопировать отчёт";
        copy.onclick = async () => {
          try {
            await navigator.clipboard.writeText(
              JSON.stringify(this.report, null, 2),
            );
            copy.textContent = "Отчёт скопирован";
          } catch {
            copy.textContent = "Выделите и скопируйте отчёт ниже";
          }
        };
        const heading = document.createElement("p");
        heading.textContent = "Полный ответ сервера";
        this.panel.append(
          title,
          this.download,
          copy,
          this.info,
          heading,
          this.raw,
        );
        root.append(this.panel);
        this.origin = performance.now();
        this.disposed = false;
        this.render();
      }
      mark(name, detail = {}) {
        (this.report.timeline ||= []).push({
          name,
          ms: Math.round(performance.now() - this.origin),
          ...detail,
        });
        this.render();
      }
      reveal() {
        this.panel.open = true;
      }
      clean(text) {
        return text.replace(
          /("(?:csrf_token|_csrf|authorization|cookie|set-cookie|api_key|password)"\s*:\s*)"(?:[^"\\]|\\.)*"/gi,
          '$1"[REDACTED]"',
        );
      }
      render() {
        if (this.disposed) return;
        this.info.textContent = JSON.stringify(this.report, null, 2);
      }
      async db() {
        return await new Promise((resolve, reject) => {
          const open = indexedDB.open("involta-voice-drafts", 1);
          open.onupgradeneeded = () =>
            open.result.createObjectStore("drafts", { keyPath: "key" });
          open.onsuccess = () => resolve(open.result);
          open.onerror = () => reject(open.error);
        });
      }
      async store(mode, value) {
        const db = await this.db();
        try {
          return await new Promise((resolve, reject) => {
            const tx = db.transaction(
              "drafts",
              mode === "get" ? "readonly" : "readwrite",
            );
            const records = tx.objectStore("drafts");
            const operation =
              mode === "put" ? records.put(value) : records.get(this.key);
            if (mode === "delete")
              operation.onsuccess = () => {
                if (operation.result?.id === value) records.delete(this.key);
              };
            tx.oncomplete = () => resolve(operation.result);
            tx.onerror = () => reject(tx.error);
            tx.onabort = () => reject(tx.error);
          });
        } finally {
          db.close();
        }
      }
      async restore() {
        try {
          const saved = await this.store("get");
          if (saved?.blob) {
            this.showAudio(saved.blob, saved.id);
            this.report.draft = {
              id: saved.id,
              bytes: saved.blob.size,
              mime: saved.blob.type,
              durationMs: saved.durationMs ?? saved.seconds * 1000,
              local_copy_saved: true,
            };
            this.render();
          }
          return saved;
        } catch (e) {
          this.report.storage_error = e.message;
          this.render();
          return null;
        }
      }
      showAudio(blob, id) {
        if (this.disposed) return;
        if (this.audioURL) URL.revokeObjectURL(this.audioURL);
        this.audioURL = URL.createObjectURL(blob);
        this.download.href = this.audioURL;
        this.download.download = `voice-${id}.${blob.type.includes("mp4") ? "m4a" : blob.type.includes("ogg") ? "ogg" : "webm"}`;
        this.download.hidden = false;
      }
      async save(blob, id, durationMs) {
        this.showAudio(blob, id);
        this.report.draft = {
          id,
          bytes: blob.size,
          mime: blob.type,
          durationMs,
          local_copy_saved: false,
        };
        try {
          await this.store("put", {
            key: this.key,
            id,
            blob,
            seconds: durationMs / 1000,
            durationMs,
            role: this.role,
            updated: Date.now(),
          });
          this.report.draft.local_copy_saved = true;
        } catch (e) {
          this.report.storage_error = e.message;
        }
        this.render();
      }
      async clear(id) {
        try {
          await this.store("delete", id);
        } catch (e) {
          this.report.storage_error = e.message;
        }
        if (this.audioURL) URL.revokeObjectURL(this.audioURL);
        this.audioURL = null;
        this.download.hidden = true;
        this.report.draft = null;
        this.render();
      }
      begin() {
        this.id = crypto.randomUUID();
        this.report.request_id = this.id;
        this.report.events = [];
        this.render();
      }
      async request(url, options = {}) {
        const start = performance.now();
        const entry = {
          request_id: this.id,
          url: url.toString(),
          method: options.method || "GET",
          started: new Date().toISOString(),
        };
        this.report.events.push(entry);
        this.render();
        const headers = new Headers(options.headers || {});
        headers.set("X-Voice-Request-ID", this.id);
        try {
          const response = await fetch(url, {
            signal: AbortSignal.timeout(90000),
            ...options,
            headers,
          });
          const body = await response.text();
          entry.elapsed_ms = Math.round(performance.now() - start);
          entry.status = response.status;
          entry.status_text = response.statusText;
          entry.final_url = response.url;
          entry.response_headers = Object.fromEntries(
            [...response.headers].filter(
              ([key]) => !/cookie|authorization|csrf/i.test(key),
            ),
          );
          entry.response_body = this.clean(body);
          this.raw.textContent = entry.response_body;
          this.render();
          let data;
          try {
            data = JSON.parse(body);
          } catch {
            throw new Error(
              `Сервер вернул неожиданный ответ (HTTP ${response.status}). Запись сохранена; полный ответ — в диагностике ниже.`,
            );
          }
          if (!response.ok) {
            const error = new Error(
              (typeof data.error === "string"
                ? data.error
                : data.error?.message) ||
                (typeof data.detail === "string"
                  ? data.detail
                  : `Не удалось отправить запись (HTTP ${response.status}).`),
            );
            error.code = data.error?.code;
            error.status = response.status;
            throw error;
          }
          return data;
        } catch (error) {
          entry.elapsed_ms = Math.round(performance.now() - start);
          entry.exception = { name: error.name, message: error.message };
          this.render();
          throw error;
        }
      }
      async publish() {
        if (!this.id) return;
        try {
          const response = await fetch(`${this.api}/debug/${this.id}`, {
            method: "POST",
            credentials: "same-origin",
            signal: AbortSignal.timeout(5000),
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ ...this.report, server: undefined }),
          });
          this.report.diagnostics_saved = response.ok;
          this.report.diagnostics_status = response.status;
        } catch (e) {
          this.report.diagnostics_saved = false;
          this.report.diagnostics_error = e.message;
        }
        this.render();
      }
      async serverTrace() {
        if (!this.id) return;
        try {
          const r = await fetch(`${this.api}/debug/${this.id}`, {
            credentials: "same-origin",
            signal: AbortSignal.timeout(5000),
          });
          if (r.ok) this.report.server = await r.json();
          else this.report.server_trace_status = r.status;
        } catch (e) {
          this.report.server_trace_error = e.message;
        }
        this.render();
      }
      dispose() {
        this.disposed = true;
        if (this.audioURL) URL.revokeObjectURL(this.audioURL);
        this.panel.remove();
      }
    }

    function mount(target, options = {}) {
      if (typeof target === "string") target = document.getElementById(target);
      if (!target || instances.has(target)) return instances.get(target);
      const root = target.shadowRoot || target.attachShadow({ mode: "open" });
      const api = new URL(
        options.apiBase || "/guide/voice/api/v1",
        location.href,
      );
      if (api.origin !== location.origin)
        throw new Error("Voice gateway must share page origin");
      const machine = new Machine();
      let disposed = false,
        session = null,
        token = 0,
        blob,
        key,
        durationMs = 0,
        previewUrl,
        reference,
        poll,
        historyPoll,
        delay = 2000,
        blocked = false;
      const style = document.createElement("style");
      style.textContent = `
        :host {
          display: block;
          inline-size: 100%;
          min-inline-size: 0;
          font: 16px/1.5 system-ui;
          color: #151515;
        }
        * {
          box-sizing: border-box;
        }
        [hidden] {
          display: none !important;
        }
        section {
          padding: 48px 0;
          background: #fff;
          text-align: center;
        }
        h2 {
          font-size: clamp(22px, 3vw, 32px);
          font-weight: 500;
          line-height: 1.28;
          max-inline-size: 760px;
          margin: 0 auto 32px;
          text-wrap: balance;
        }
        .stage {
          min-block-size: 248px;
          display: grid;
          grid-template-rows: 80px 28px 28px 28px 44px;
          grid-template-columns: 1fr;
          justify-items: center;
          align-content: start;
          gap: 4px;
        }
        .primary {
          position: relative;
          inline-size: 80px;
          block-size: 80px;
          border: 0;
          border-radius: 50%;
          background: #111;
          color: white;
          padding: 0;
          display: grid;
          place-items: center;
          touch-action: manipulation;
          cursor: pointer;
          isolation: isolate;
        }
        .primary::before {
          content: "";
          position: absolute;
          inset: -7px;
          border: 1px solid #bbb;
          border-radius: 50%;
          opacity: 0;
          transform: scale(1);
          pointer-events: none;
          transition:
            transform 0.1s,
            opacity 0.1s;
        }
        .primary.recording::before {
          opacity: 0.65;
          transform: scale(calc(1 + var(--level, 0) * 0.2));
        }
        .primary.waiting svg {
          animation: waiting 1.2s linear infinite;
        }
        .primary svg {
          width: 28px;
          height: 28px;
          fill: none;
          stroke: currentColor;
          stroke-width: 1.8;
        }
        .primary:disabled {
          cursor: wait;
          opacity: 0.5;
        }
        .stage > * {
          grid-column: 1;
        }
        .primary {
          grid-row: 1;
        }
        .caption {
          grid-row: 2;
          font-size: 15px;
        }
        .cancel {
          grid-row: 5;
        }
        .timer {
          grid-row: 3;
          font-variant-numeric: tabular-nums;
          font-size: 18px;
        }
        .hint {
          font-size: 13px;
          color: #666;
        }
        .quiet {
          visibility: hidden;
        }
        .cancel,
        .secondary {
          font: inherit;
          background: none;
          border: 0;
          text-decoration: underline;
          text-underline-offset: 4px;
          cursor: pointer;
          padding: 10px 16px;
          min-block-size: 44px;
          color: inherit;
        }
        .preview {
          min-block-size: 248px;
          display: flex;
          flex-direction: column;
          align-items: center;
          gap: 12px;
        }
        audio {
          display: block;
          inline-size: min(100%, 560px);
          margin: 0 auto 10px;
        }
        .send {
          min-block-size: 48px;
          padding: 12px 28px;
          background: #111;
          color: #fff;
          border: 1px solid #111;
          font: inherit;
          cursor: pointer;
        }
        button:disabled {
          opacity: 0.5;
          cursor: not-allowed;
        }
        button:focus-visible,
        a:focus-visible,
        summary:focus-visible {
          outline: 3px solid #606060;
          outline-offset: 5px;
        }
        .status,
        .error,
        .vad-status,
        .current,
        .history {
          max-inline-size: 720px;
          margin: 12px auto;
          overflow-wrap: anywhere;
        }
        .error {
          color: #922626;
        }
        .vad-status {
          font-size: 13px;
          color: #666;
          min-block-size: 20px;
        }
        .history {
          text-align: start;
          white-space: pre-wrap;
        }
        details {
          text-align: start;
          max-inline-size: 760px;
          margin: 28px auto 0;
          font-size: 13px;
        }
        summary {
          cursor: pointer;
          min-block-size: 44px;
          padding: 10px 0;
        }
        details button {
          padding: 10px;
          min-block-size: 44px;
          margin: 4px;
        }
        details a {
          display: inline-block;
          margin: 8px;
        }
        pre {
          max-inline-size: 100%;
          font-size: 12px;
        }
        @keyframes waiting {
          to {
            transform: rotate(360deg);
          }
        }
        @media (prefers-reduced-motion: reduce) {
          *,
          *::before {
            animation: none !important;
            transition: none !important;
          }
          .primary.recording::before {
            transform: none;
          }
        }
      `;
      const panel = document.createElement("section");
      panel.setAttribute("aria-label", "Голосовой отзыв");
      const heading = document.createElement("h2");
      heading.textContent = REQUEST;
      const stage = document.createElement("div");
      stage.className = "stage";
      const primary = document.createElement("button");
      primary.type = "button";
      primary.className = "primary";
      const caption = document.createElement("span");
      caption.className = "caption";
      const timerText = document.createElement("span");
      timerText.className = "timer";
      timerText.setAttribute("aria-hidden", "true");
      const hint = document.createElement("span");
      hint.className = "hint";
      const cancel = document.createElement("button");
      cancel.type = "button";
      cancel.className = "cancel";
      cancel.textContent = "Отменить";
      stage.append(primary, caption, timerText, hint, cancel);
      const preview = document.createElement("div");
      preview.className = "preview";
      preview.hidden = true;
      const status = document.createElement("p");
      status.className = "status";
      status.setAttribute("role", "status");
      const error = document.createElement("p");
      error.className = "error";
      error.setAttribute("role", "alert");
      const vadStatus = document.createElement("p");
      vadStatus.className = "vad-status";
      const history = document.createElement("div");
      history.className = "history";
      const currentText = document.createElement("p");
      currentText.className = "current";
      panel.append(
        heading,
        stage,
        preview,
        status,
        error,
        vadStatus,
        currentText,
        history,
      );
      root.replaceChildren(style, panel);
      const diagnostics = new Diagnostics(panel, {
        key: "voice",
        api: api.href,
        role: options.actor || "visitor",
        location: options.location || "",
      });
      const mic =
        '<svg aria-hidden="true" viewBox="0 0 28 28"><rect x="10" y="3" width="8" height="14" rx="4"/><path d="M6 12v2a8 8 0 0 0 16 0v-2M14 22v4M9 26h10"/></svg>';
      const square =
        '<svg aria-hidden="true" viewBox="0 0 28 28"><rect x="6" y="6" width="16" height="16" rx="1" fill="currentColor" stroke="none"/></svg>';
      const spinner =
        '<svg aria-hidden="true" viewBox="0 0 28 28"><path d="M24 14A10 10 0 1 1 14 4"/></svg>';
      function button(label, fn, className) {
        const b = document.createElement("button");
        b.type = "button";
        b.textContent = label;
        b.className = className;
        b.onclick = fn;
        return b;
      }
      function refreshAll() {
        for (const i of instances.values()) i.refresh();
      }
      function transition(next) {
        machine.move(next);
        render();
      }
      function clearBlob() {
        if (previewUrl) URL.revokeObjectURL(previewUrl);
        previewUrl = null;
        blob = null;
        key = null;
        durationMs = 0;
        blocked = false;
      }
      function render() {
        if (disposed) return;
        const state = machine.state,
          recording = state === "recording",
          waiting = state === "starting" || state === "stopping";
        target.dataset.voiceState = state;
        const showingPreview =
          !!blob && ["preview", "retry", "uploading"].includes(state);
        stage.hidden = showingPreview;
        preview.hidden = !showingPreview;
        primary.classList.toggle("recording", recording);
        primary.classList.toggle("waiting", waiting);
        primary.setAttribute(
          "aria-label",
          recording ? "Остановить запись" : "Записать отзыв",
        );
        primary.setAttribute("aria-busy", String(waiting));
        primary.disabled =
          state === "stopping" ||
          state === "uploading" ||
          !!(activeCapture && activeCapture !== instance);
        primary.setAttribute(
          "aria-disabled",
          String(waiting || primary.disabled),
        );
        const icon = recording ? square : waiting ? spinner : mic;
        const iconState = recording
          ? "stop"
          : waiting
            ? "waiting"
            : "microphone";
        if (primary.dataset.icon !== iconState) {
          primary.dataset.icon = iconState;
          primary.innerHTML = icon;
        }
        caption.textContent = recording
          ? "Стоп"
          : waiting
            ? state === "starting"
              ? "Разреши доступ к микрофону"
              : "Сохраняем запись"
            : "Записать отзыв";
        const elapsed =
          recording && session
            ? Math.max(0, performance.now() - session.started)
            : 0;
        const timerValue = recording
          ? `${String(Math.floor(elapsed / 60000)).padStart(2, "0")}:${String(Math.floor(elapsed / 1000) % 60).padStart(2, "0")}`
          : "";
        if (timerText.textContent !== timerValue)
          timerText.textContent = timerValue;
        hint.textContent = recording ? "Минимум 5 секунд" : "От 5 секунд";
        hint.style.gridRow = recording ? "4" : "3";
        hint.classList.toggle(
          "quiet",
          waiting || (recording && elapsed >= MIN_MS),
        );
        cancel.classList.toggle(
          "quiet",
          !["starting", "recording"].includes(state),
        );
        cancel.disabled = !["starting", "recording"].includes(state);
        if (
          showingPreview &&
          preview.dataset.key !== `${key}:${state}:${blocked}`
        ) {
          preview.dataset.key = `${key}:${state}:${blocked}`;
          preview.replaceChildren();
          if (!previewUrl) previewUrl = URL.createObjectURL(blob);
          const player = document.createElement("audio");
          player.controls = true;
          player.src = previewUrl;
          player.setAttribute("aria-label", "Прослушать запись");
          const sendButton = button(
            state === "uploading" ? "Отправляем…" : "Отправить отзыв",
            send,
            "send",
          );
          sendButton.disabled = state === "uploading" || blocked;
          const again = button(
            "Записать заново",
            () => start(true),
            "secondary",
          );
          again.disabled = state === "uploading" || !!activeCapture;
          const length = document.createElement("span");
          length.className = "timer";
          length.textContent = `${String(Math.floor(durationMs / 60000)).padStart(2, "0")}:${String(Math.floor(durationMs / 1000) % 60).padStart(2, "0")}`;
          preview.append(player, length, sendButton, again);
        }
      }
      function release(s, unlock = true) {
        clearInterval(s.timer);
        clearTimeout(s.vadTimeout);
        clearTimeout(s.finalizeTimeout);
        clearTimeout(s.analysisTimer);
        cancelAnimationFrame(s.analysisFrame);
        s.stream?.getTracks().forEach((t) => t.stop());
        s.source?.disconnect();
        if (s.vad) void destroyVad(s.vad);
        if (s.context && s.context.state !== "closed")
          void s.context.close().catch(() => {});
        if (unlock && activeCapture === instance) {
          activeCapture = null;
          refreshAll();
        }
      }
      function vadFallback(s, reason, cause = null) {
        if (session !== s || s.vadDisabled) return;
        s.vadDisabled = true;
        clearTimeout(s.vadTimeout);
        diagnostics.report.vad = {
          state: "unavailable",
          reason,
          error: cause
            ? { name: cause.name, message: cause.message, stack: cause.stack }
            : null,
        };
        diagnostics.mark("vad_unavailable", { reason });
        vadStatus.textContent =
          "Автостоп недоступен. Останови запись кнопкой «Стоп».";
        if (s.vad) void destroyVad(s.vad);
      }
      function initAnalysis(s) {
        if (session !== s || s.ending || disposed) return;
        try {
          const Audio = window.AudioContext || window.webkitAudioContext;
          if (!Audio) {
            vadFallback(s, "audio_context_unavailable");
            return;
          }
          s.context = new Audio();
          void s.context
            .resume()
            .catch((error) => vadFallback(s, "audio_context_suspended", error));
          s.source = s.context.createMediaStreamSource(s.stream);
          s.analyser = s.context.createAnalyser();
          s.analyser.fftSize = 512;
          s.source.connect(s.analyser);
          void startVad(s);
        } catch (error) {
          vadFallback(s, "audio_context_unavailable", error);
        }
      }
      async function startVad(s) {
        if (!s.context) {
          vadFallback(s, "audio_context_unavailable");
          return;
        }
        diagnostics.report.vad = {
          state: "loading",
          model: "Silero v5",
          positive: 0.3,
          negative: 0.25,
        };
        s.vadTimeout = setTimeout(() => vadFallback(s, "load_timeout"), 12000);
        try {
          await loadRuntime();
          if (session !== s || s.ending || s.vadDisabled) return;
          const vad = await window.vad.MicVAD.new({
            model: "v5",
            baseAssetPath: assetBase,
            onnxWASMBasePath: assetBase,
            audioContext: s.context,
            getStream: async () => s.stream,
            pauseStream: async () => {},
            resumeStream: async () => s.stream,
            startOnLoad: false,
            positiveSpeechThreshold: 0.3,
            negativeSpeechThreshold: 0.25,
            minSpeechMs: 400,
            ortConfig: (ort) => {
              ort.env.wasm.numThreads = 1;
              ort.env.wasm.proxy = false;
            },
            onFrameProcessed: (probabilities, frame) => {
              if (
                session !== s ||
                s.ending ||
                s.vadDisabled ||
                machine.state !== "recording"
              )
                return;
              if (s.context.state !== "running") {
                vadFallback(s, "audio_context_suspended");
                return;
              }
              const now = performance.now(),
                frameMs = frame.length / 16;
              s.firstFrame ??= now;
              s.frameClock = (s.frameClock || 0) + frameMs;
              if (
                (s.lastFrame && now - s.lastFrame > 250) ||
                Math.abs(now - s.firstFrame - s.frameClock) > 250
              ) {
                vadFallback(s, "frame_lag");
                return;
              }
              s.lastFrame = now;
              const result = s.gate.frame(
                probabilities.isSpeech,
                s.frameClock,
                frameMs,
              );
              if (result.unavailable) {
                vadFallback(s, "frame_lag");
                return;
              }
              if (!s.vadReady) {
                s.vadReady = true;
                clearTimeout(s.vadTimeout);
                diagnostics.report.vad.state = "ready";
                diagnostics.mark("vad_ready");
              }
              if (result.speech) diagnostics.mark("vad_speech_confirmed");
              if (s.gate.spoken) {
                const frameState =
                  probabilities.isSpeech >= 0.3
                    ? "speech"
                    : probabilities.isSpeech <= 0.25
                      ? "silence"
                      : "uncertain";
                if (frameState !== s.frameState)
                  diagnostics.mark("vad_" + frameState, {
                    processed_ms: s.gate.processed,
                  });
                s.frameState = frameState;
              }
              if (result.warning !== s.warning) {
                s.warning = result.warning;
                diagnostics.mark(
                  result.warning ? "vad_pause_warning" : "vad_pause_reset",
                );
              }
              vadStatus.textContent = result.warning
                ? "Пауза — сейчас запись остановится."
                : "";
              if (result.stop) stop(result.stop);
            },
          });
          if (session !== s || s.ending || s.vadDisabled) {
            await destroyVad(vad);
            return;
          }
          s.vad = vad;
          await vad.start();
        } catch (e) {
          if (session === s && !s.ending) vadFallback(s, "load_failure", e);
        }
      }
      function tick(s) {
        if (session !== s || machine.state !== "recording") return;
        render();
        if (performance.now() - s.started >= MAX_MS) {
          stop("limit");
          return;
        }
        if (
          s.vadReady &&
          (s.context?.state !== "running" ||
            performance.now() - s.lastFrame > 750)
        )
          vadFallback(s, "stream_stalled");
        if (s.analyser) {
          const values = new Float32Array(s.analyser.fftSize);
          s.analyser.getFloatTimeDomainData(values);
          const rms = Math.sqrt(
            values.reduce((sum, v) => sum + v * v, 0) / values.length,
          );
          primary.style.setProperty("--level", String(Math.min(1, rms * 8)));
        }
      }
      async function start(replace = false) {
        if (
          disposed ||
          !["idle", "preview", "retry", "accepted", "error"].includes(
            machine.state,
          ) ||
          activeCapture
        )
          return;
        error.textContent = "";
        status.textContent = "";
        vadStatus.textContent = "";
        const s = {
          token: ++token,
          id: crypto.randomUUID(),
          ending: false,
          chunks: [],
          bytes: 0,
          gate: new SilenceGate(),
          warning: false,
        };
        session = s;
        activeCapture = instance;
        diagnostics.origin = performance.now();
        diagnostics.report.timeline = [];
        diagnostics.mark("press");
        transition("starting");
        refreshAll();
        requestAnimationFrame(() => {
          if (session === s) diagnostics.mark("controls_painted");
        });
        const replacement =
          replace && key ? diagnostics.clear(key) : Promise.resolve();
        if (replace) clearBlob();
        if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
          failStart(s, "Запись недоступна. Нужен современный браузер и HTTPS.");
          return;
        }
        try {
          diagnostics.mark("permission_request");
          const incoming = await navigator.mediaDevices.getUserMedia({
            audio: true,
          });
          if (disposed || session !== s || s.token !== token) {
            incoming.getTracks().forEach((t) => t.stop());
            return;
          }
          diagnostics.mark("permission_resolved");
          s.stream = incoming;
          // A late IndexedDB restore must never be overwritten by a short/cancelled new attempt.
          await replacement;
          let saved = null;
          try {
            saved = await diagnostics.store("get");
          } catch {
            /* local storage failure is surfaced on save */
          }
          if (session !== s || disposed) {
            incoming.getTracks().forEach((t) => t.stop());
            return;
          }
          if (saved?.blob && !replace && !blob) {
            release(s);
            session = null;
            restoreBlob(saved);
            transition("preview");
            return;
          }
          clearBlob();
          const mime = [
            "audio/webm;codecs=opus",
            "audio/mp4",
            "audio/ogg;codecs=opus",
          ].find((t) => MediaRecorder.isTypeSupported(t));
          s.recorder = new MediaRecorder(
            incoming,
            mime ? { mimeType: mime } : undefined,
          );
          incoming.getTracks().forEach((track) =>
            track.addEventListener("ended", () => {
              if (session === s && !s.ending) interrupt("track_ended");
            }),
          );
          s.recorder.ondataavailable = (event) => {
            if (session !== s || s.token !== token || !event.data.size) return;
            s.chunks.push(event.data);
            s.bytes += event.data.size;
            if (s.bytes > MAX_BYTES) stop("size");
          };
          s.recorder.onerror = (event) => {
            if (session === s) {
              diagnostics.mark("recorder_error", {
                name: event.error?.name,
                message: event.error?.message,
              });
              stop("recorder_error");
            }
          };
          s.recorder.onstart = () => {
            if (session !== s || s.token !== token || disposed) return;
            s.started = performance.now();
            diagnostics.mark("recorder_start");
            status.textContent = "Идёт запись.";
            transition("recording");
            s.timer = setInterval(() => tick(s), 100);
            s.analysisFrame = requestAnimationFrame(() => {
              s.analysisTimer = setTimeout(() => initAnalysis(s), 0);
            });
          };
          s.recorder.onstop = () => {
            if (!s.ending && session === s) {
              s.ending = true;
              s.reason =
                machine.state === "recording"
                  ? "recorder_stopped"
                  : "recorder_error";
              s.duration = Math.max(0, performance.now() - s.started);
              diagnostics.mark("stop_requested", {
                reason: s.reason,
                duration_ms: s.duration,
              });
              transition("stopping");
            }
            void finalize(s);
          };
          s.started = performance.now();
          diagnostics.mark("recorder_start_requested");
          s.recorder.start(250);
        } catch (e) {
          if (session === s && s.token === token)
            failStart(
              s,
              e.name === "NotAllowedError"
                ? "Нет доступа к микрофону. Разреши запись в настройках браузера."
                : "Не удалось включить микрофон. Попробуй ещё раз.",
              e,
            );
        }
      }
      function failStart(s, message, cause = null) {
        release(s);
        session = null;
        error.textContent = message;
        diagnostics.mark("capture_error", {
          error: cause
            ? { name: cause.name, message: cause.message, stack: cause.stack }
            : null,
        });
        diagnostics.reveal();
        transition("error");
      }
      function stop(reason = "manual") {
        const s = session;
        if (
          !s ||
          s.ending ||
          !["recording", "starting"].includes(machine.state) ||
          !s.recorder
        )
          return;
        s.ending = true;
        s.reason = reason;
        s.stopped = performance.now();
        s.duration = Math.max(0, s.stopped - s.started);
        diagnostics.mark("stop_requested", { reason, duration_ms: s.duration });
        transition("stopping");
        clearInterval(s.timer);
        clearTimeout(s.vadTimeout);
        if (s.recorder.state !== "inactive") s.recorder.stop();
        s.stream.getTracks().forEach((t) => t.stop());
        s.finalizeTimeout = setTimeout(() => {
          if (session === s) {
            ++token;
            release(s);
            session = null;
            error.textContent =
              "Не удалось завершить кодирование записи. Запиши заново.";
            diagnostics.mark("encoding_timeout");
            diagnostics.reveal();
            transition("error");
          }
        }, 5000);
      }
      async function finalize(s) {
        if (s.finalized) return;
        s.finalized = true;
        release(s, false);
        diagnostics.mark("stop_completed", { reason: s.reason });
        if (session !== s || s.token !== token) {
          if (activeCapture === instance) {
            activeCapture = null;
            refreshAll();
          }
          return;
        }
        if (s.cancelled) {
          session = null;
          return;
        }
        const recorded = new Blob(s.chunks, {
          type: s.recorder.mimeType || "audio/webm",
        });
        diagnostics.mark("blob_ready", {
          bytes: recorded.size,
          mime: recorded.type,
          duration_ms: s.duration,
          reason: s.reason,
        });
        session = null;
        if (
          s.reason === "size" ||
          s.reason === "recorder_error" ||
          !Number.isFinite(s.duration) ||
          !recorded.size
        ) {
          error.textContent =
            s.reason === "size"
              ? "Запись больше 10 МБ. Запиши более короткий отзыв."
              : "Не удалось завершить аудиозапись. Запиши заново.";
          release(s);
          diagnostics.reveal();
          if (!disposed) transition("error");
          return;
        }
        if (s.duration < MIN_MS) {
          release(s);
          diagnostics.mark("too_short", { duration_ms: s.duration });
          if (!disposed) {
            status.textContent = TOO_SHORT;
            vadStatus.textContent = "";
            transition("idle");
          }
          return;
        }
        blob = recorded;
        key = s.id;
        durationMs = s.duration;
        await diagnostics.save(blob, key, durationMs);
        release(s);
        if (disposed) return;
        status.textContent = diagnostics.report.draft?.local_copy_saved
          ? "Запись сохранена в браузере. Прослушай и отправь отзыв."
          : "Запись готова. Скачай аудио перед закрытием страницы: локальное сохранение недоступно.";
        vadStatus.textContent = "";
        transition("preview");
      }
      function cancelAttempt() {
        const s = session;
        ++token;
        if (s) {
          s.cancelled = true;
          s.ending = true;
          if (s.recorder?.state !== "inactive" && s.recorder) s.recorder.stop();
          release(s);
          session = null;
          diagnostics.mark("cancel");
        }
        status.textContent = "";
        error.textContent = "";
        vadStatus.textContent = "";
        transition("idle");
      }
      function interrupt(reason) {
        if (machine.state === "starting") cancelAttempt();
        else if (machine.state === "recording") stop(reason);
      }
      function primaryAction() {
        if (machine.state === "recording") stop("manual");
        else if (["idle", "accepted", "error"].includes(machine.state))
          void start();
      }
      primary.addEventListener("pointerdown", (event) => {
        if (!event.isPrimary || event.button !== 0) return;
        primaryAction();
      });
      primary.addEventListener("click", (event) => {
        if (event.detail === 0) primaryAction();
      });
      primary.addEventListener("keydown", (event) => {
        if (event.repeat && ["Enter", " "].includes(event.key))
          event.preventDefault();
      });
      primary.addEventListener("pointercancel", () =>
        interrupt("pointercancel"),
      );
      cancel.onclick = cancelAttempt;
      const escape = (event) => {
        if (event.key === "Escape" && activeCapture === instance) {
          event.preventDefault();
          cancelAttempt();
        }
      };
      const visibility = () => {
        if (document.hidden && activeCapture === instance)
          interrupt("visibilitychange");
      };
      const pagehide = () => {
        if (activeCapture === instance) interrupt("pagehide");
      };
      document.addEventListener("keydown", escape);
      document.addEventListener("visibilitychange", visibility);
      window.addEventListener("pagehide", pagehide);
      async function send() {
        if (
          !blob ||
          !key ||
          blocked ||
          durationMs < MIN_MS ||
          !["preview", "retry"].includes(machine.state)
        )
          return;
        error.textContent = "";
        transition("uploading");
        diagnostics.begin();
        diagnostics.mark("upload_requested");
        const params = new URLSearchParams({
          location: options.location || "",
          idempotency_key: key,
          language: navigator.language || "",
          timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "",
        });
        try {
          const identity = await diagnostics.request(api.href + "/identity", {
            method: "POST",
            credentials: "same-origin",
          });
          if (!identity.ready)
            throw new Error("Не удалось подготовить отправку записи.");
          if (identity.actor && identity.actor !== diagnostics.role)
            throw new Error(
              "Режим входа изменился. Вернись в прежний режим — запись сохранена.",
            );
          const data = await diagnostics.request(
            api.href + "/submissions?" + params,
            {
              method: "POST",
              credentials: "same-origin",
              body: blob,
              headers: {
                "Content-Type": blob.type || "audio/webm",
                "X-CSRF-Token": identity.csrf_token || "",
              },
            },
          );
          if (!data.reference || typeof data.reference !== "string")
            throw new Error(
              "Сервер не подтвердил сохранение. Запись остаётся в браузере.",
            );
          if (disposed) return;
          reference = data.reference;
          diagnostics.mark("upload_accepted");
          await diagnostics.publish();
          await diagnostics.clear(key);
          clearBlob();
          transition("accepted");
          status.textContent = "Запись доставлена. Расшифровываем твой отзыв.";
          delay = 2000;
          schedule();
        } catch (e) {
          if (disposed) return;
          blocked = e.code === "audio_too_short";
          error.textContent =
            e.name === "TypeError"
              ? "Связь прервалась. Запись сохранена — повтори отправку."
              : e.message || "Не удалось отправить запись.";
          transition("retry");
          diagnostics.reveal();
          await diagnostics.publish();
          await diagnostics.serverTrace();
        }
      }
      const labels = {
        pending: "Ожидает редактора",
        approved: "Принято",
        rejected: "Отклонено",
        fraud: "Помечено как фрод",
        published: "Опубликовано",
      };
      function schedule() {
        clearTimeout(poll);
        if (!disposed) poll = setTimeout(pollStatus, delay);
      }
      async function pollStatus() {
        if (disposed) return;
        if (document.hidden) {
          schedule();
          return;
        }
        try {
          const response = await fetch(
            api.href + "/submissions/" + encodeURIComponent(reference),
            { credentials: "same-origin" },
          );
          if (!response.ok) throw new Error("status");
          const data = await response.json();
          if (disposed) return;
          status.textContent = data.transcript
            ? "Текст отзыва сохранён. " +
              (labels[data.editorial_state || data.moderation] ||
                "Ожидает редактора")
            : "Запись доставлена. Обрабатываем.";
          currentText.textContent = data.transcript || "";
          if (data.state.includes("failed") || data.state.includes("unknown")) {
            status.textContent = data.transcript
              ? "Текст отзыва сохранён и доступен редактору. Дополнительный анализ не завершён."
              : "Запись сохранена. Расшифровка не завершена; редактор может проверить ошибку.";
            return;
          }
          if (
            ["approved", "published", "rejected", "fraud"].includes(
              data.editorial_state || data.moderation,
            )
          )
            return;
          delay = Math.min(delay * 1.5, 60000);
          schedule();
        } catch {
          delay = Math.min(delay * 2, 60000);
          schedule();
        }
      }
      async function loadHistory() {
        if (disposed) return;
        if (document.hidden) {
          historyPoll = setTimeout(loadHistory, 15000);
          return;
        }
        let ongoing = false;
        try {
          const response = await fetch(
            api.href +
              "/submissions/mine?location=" +
              encodeURIComponent(options.location || ""),
            { credentials: "same-origin" },
          );
          if (!response.ok) return;
          const data = await response.json();
          if (disposed) return;
          history.replaceChildren();
          if (data.rows?.length) {
            const title = document.createElement("h3");
            title.textContent = "Ваши отзывы";
            history.append(title);
          }
          for (const item of data.rows || []) {
            const paragraph = document.createElement("p");
            const state = item.editorial_state || item.moderation;
            paragraph.textContent =
              (labels[state] || "Обрабатывается") +
              (item.transcript ? ": " + item.transcript : "");
            history.append(paragraph);
            ongoing ||= ![
              "approved",
              "published",
              "rejected",
              "fraud",
            ].includes(state);
          }
        } catch {
          ongoing = true;
        }
        if (ongoing && !disposed) historyPoll = setTimeout(loadHistory, 15000);
      }
      function restoreBlob(saved) {
        blob = saved.blob;
        key = saved.id;
        durationMs = saved.durationMs ?? saved.seconds * 1000;
        blocked = !Number.isFinite(durationMs) || durationMs < MIN_MS;
        status.textContent = blocked
          ? "Сохранённая запись короче 5 секунд. Прослушай её и запиши отзыв заново."
          : "Восстановлена неотправленная запись. Можно прослушать и отправить её.";
        if (blocked) diagnostics.reveal();
      }
      function unmount() {
        if (disposed) return;
        if (machine.state === "recording") stop("unmount");
        else if (machine.state === "starting") cancelAttempt();
        disposed = true;
        clearTimeout(poll);
        clearTimeout(historyPoll);
        document.removeEventListener("keydown", escape);
        document.removeEventListener("visibilitychange", visibility);
        window.removeEventListener("pagehide", pagehide);
        if (session) release(session);
        clearBlob();
        diagnostics.dispose();
        root.replaceChildren();
        instances.delete(target);
      }
      const instance = {
        unmount,
        refresh: render,
        get state() {
          return machine.state;
        },
      };
      instances.set(target, instance);
      render();
      loadHistory();
      const restored = diagnostics.restore();
      restored.then((saved) => {
        if (!disposed && saved?.blob && machine.state === "idle") {
          restoreBlob(saved);
          transition("preview");
        }
      });
      return instance;
    }
    window.InvoltaVoice = {
      mount,
      unmount(target) {
        instances
          .get(
            typeof target === "string"
              ? document.getElementById(target)
              : target,
          )
          ?.unmount();
      },
      Machine,
      SilenceGate,
    };
  }
  if (script?.dataset.target)
    window.InvoltaVoice.mount(script.dataset.target, {
      apiBase: script.dataset.apiBase,
      location: script.dataset.location,
      actor: script.dataset.actor,
    });
})();
