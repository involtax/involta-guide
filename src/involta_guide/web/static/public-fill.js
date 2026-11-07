// A click starts generation; subsequent requests only read the committed city state.
(() => {
  const panel = document.querySelector('[data-public-fill]');
  if (!panel) return;
  const root = panel.closest('[data-fill-state]');
  const form = panel.querySelector('form');
  const button = form.querySelector('button');
  const waiting = panel.querySelector('[data-fill-waiting]');
  const message = panel.querySelector('[data-fill-message]');
  const status = panel.querySelector('[role="status"]');
  const canvas = root.querySelector('[data-fill-spoiler]');
  const motion = matchMedia('(prefers-reduced-motion: reduce)');
  const messages = [
    'Запускаем гастрономическую разведку.',
    'Сканируем город и его окрестности.',
    'Собираем сигналы: меню, отзывы, репутацию.',
    'Фильтруем шум. Выделяем сильные места.',
    'Сопоставляем кухню, сервис и атмосферу.',
    'Сводим лучшие находки в короткий список.',
    'Собираем вашу подборку из проверенных мест.',
  ];
  const pending = state => state === 'queued' || state === 'running';
  const waitingStatus = 'Идёт поиск ресторанов. Готовая подборка появится автоматически.';
  let busy = false;
  let finished = false;
  let pollTimer;
  let messageTimer;
  let fadeTimer;
  let messageIndex = 0;
  let frame;
  let lastFrame = 0;
  let particles = [];
  // No canvas support must not prevent status polling or publication.
  const context = canvas.getContext('2d');

  function drawSpoiler(time = 0) {
    if (!context) return;
    context.clearRect(0, 0, canvas.clientWidth, canvas.clientHeight);
    const seconds = motion.matches ? 0 : time / 1000;
    for (const p of particles) {
      const x = p.x + Math.sin(seconds * p.speed + p.phase) * 1.7;
      const y = p.y + Math.cos(seconds * p.speed * 0.8 + p.phase) * 1.2;
      const opacity = p.opacity * (0.72 + 0.28 * Math.sin(seconds * 1.5 + p.phase));
      context.beginPath();
      context.arc(x, y, p.radius, 0, Math.PI * 2);
      context.fillStyle = `rgba(21,21,21,${opacity})`;
      context.fill();
    }
  }
  function resizeSpoiler() {
    if (!busy || !context) return;
    const width = canvas.clientWidth;
    const ratio = Math.min(devicePixelRatio || 1, 2);
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(canvas.clientHeight * ratio);
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    const lines = width < 400 ? [0.97, 0.88, 0.96, 0.61] : [0.96, 0.91, 0.64];
    particles = [];
    lines.forEach((length, row) => {
      for (let i = 0; i < width * length * 1.75; i++) {
        particles.push({
          x: Math.random() * width * length,
          y: row * 27 + 8 + Math.random() * 12,
          radius: 0.45 + Math.random() * 0.7,
          opacity: 0.12 + Math.random() * 0.35,
          phase: Math.random() * Math.PI * 2,
          speed: 0.4 + Math.random() * 0.8,
        });
      }
    });
    drawSpoiler();
  }
  function animateSpoiler(time) {
    if (!busy || motion.matches || document.hidden || !context) return;
    if (time - lastFrame > 40) {
      drawSpoiler(time);
      lastFrame = time;
    }
    frame = requestAnimationFrame(animateSpoiler);
  }
  function resumeAnimation() {
    cancelAnimationFrame(frame);
    if (busy) {
      drawSpoiler();
      if (!motion.matches && !document.hidden) frame = requestAnimationFrame(animateSpoiler);
    }
  }
  function rotateMessage() {
    if (document.hidden) return;
    messageIndex = (messageIndex + 1) % messages.length;
    message.classList.add('is-changing');
    fadeTimer = setTimeout(() => {
      message.textContent = messages[messageIndex];
      message.classList.remove('is-changing');
    }, motion.matches ? 0 : 230);
  }
  function stopWaiting() {
    busy = false;
    clearTimeout(pollTimer);
    clearInterval(messageTimer);
    clearTimeout(fadeTimer);
    cancelAnimationFrame(frame);
  }
  function showPending(state, focus = false) {
    root.dataset.fillState = state;
    root.querySelector('.public-fill-intro').setAttribute('aria-busy', 'true');
    form.hidden = true;
    button.disabled = true;
    waiting.hidden = false;
    status.classList.add('public-fill-sr');
    if (status.textContent !== waitingStatus) status.textContent = waitingStatus;
    if (!busy) {
      busy = true;
      messageIndex = 0;
      message.textContent = messages[0];
      message.classList.remove('is-changing');
      messageTimer = setInterval(rotateMessage, 5000);
      resizeSpoiler();
      resumeAnimation();
    }
    if (focus) waiting.focus({preventScroll: true});
  }
  function display(data) {
    if (data.state === 'ready' || data.state === 'existing') {
      finished = true;
      stopWaiting();
      // The server returns ready only after the location and restaurants are committed.
      window.location.reload();
      return;
    }
    if (pending(data.state)) {
      showPending(data.state);
      schedulePoll();
      return;
    }
    stopWaiting();
    root.dataset.fillState = data.state;
    root.querySelector('.public-fill-intro').setAttribute('aria-busy', 'false');
    waiting.hidden = true;
    form.hidden = data.can_start !== true;
    button.disabled = data.can_start !== true;
    status.classList.remove('public-fill-sr');
    status.textContent = data.message;
  }
  function schedulePoll() {
    clearTimeout(pollTimer);
    if (!finished && busy) pollTimer = setTimeout(poll, 3000);
  }
  async function poll() {
    try {
      const response = await fetch(panel.dataset.statusUrl, {
        cache: 'no-store', signal: AbortSignal.timeout(10000),
      });
      if (response.status >= 400 && response.status < 500 && ![408, 429].includes(response.status)) {
        display({state: 'unavailable', can_start: false,
          message: 'Не удалось проверить готовность подборки. Обновите страницу.'});
        return;
      }
      if (!response.ok) throw new Error('Status temporarily unavailable');
      const data = await response.json();
      if (typeof data.state !== 'string') throw new Error('Missing fill state');
      display(data);
    } catch {
      // A lost connection is not a failed job. Keep reading; never repeat the POST.
      schedulePoll();
    }
  }
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (busy || finished) return;
    showPending('queued', true);
    try {
      const response = await fetch(form.action, {
        method: 'POST', headers: {'Accept': 'application/json'},
        signal: AbortSignal.timeout(10000),
      });
      if (!response.ok) throw new Error('Submission outcome unknown');
      const data = await response.json();
      if (typeof data.state !== 'string') throw new Error('Missing fill state');
      display(data);
    } catch {
      // The POST may have committed even if its response was lost.
      await poll();
    }
  });
  addEventListener('resize', resizeSpoiler);
  document.addEventListener('visibilitychange', resumeAnimation);
  motion.addEventListener('change', resumeAnimation);
  addEventListener('pagehide', stopWaiting);
  addEventListener('pageshow', event => {
    if (event.persisted && pending(root.dataset.fillState)) {
      showPending(root.dataset.fillState);
      poll();
    }
  });
  if (pending(root.dataset.fillState)) {
    showPending(root.dataset.fillState);
    poll();
  } else if (root.dataset.fillState === 'ready' || root.dataset.fillState === 'existing') {
    // Publication can commit between reading page content and reading fill status.
    display({state: root.dataset.fillState});
  }
})();
