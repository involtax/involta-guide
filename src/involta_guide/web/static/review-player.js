// The media element owns playback state. Buffering never paints listened progress.
(() => {
  const players = new Map();
  const stamp = value => {
    const seconds = Math.max(0, Math.floor(value || 0));
    return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
  };

  function mount(root) {
    const audio = root.querySelector('audio');
    const controls = root.querySelector('.review-player-controls');
    const button = root.querySelector('.review-play');
    const seek = root.querySelector('.review-seek');
    const time = root.querySelector('.review-time');
    const error = root.querySelector('.review-player-error');
    const events = new AbortController();
    let frame = 0;
    let pending = false;
    const listen = (target, name, handler) => target.addEventListener(name, handler, {signal: events.signal});

    function draw() {
      const duration = Number.isFinite(audio.duration) && audio.duration > 0 ? audio.duration : 0;
      const position = Math.max(0, audio.currentTime || 0);
      seek.disabled = !duration || Boolean(audio.error);
      seek.max = duration || 1;
      seek.value = Math.min(position, duration);
      seek.style.setProperty('--progress', `${duration ? Math.min(position / duration, 1) * 100 : 0}%`);
      const spokenTime = `${stamp(position)} из ${duration ? stamp(duration) : 'неизвестно'}`;
      if (seek.getAttribute('aria-valuetext') !== spokenTime) seek.setAttribute('aria-valuetext', spokenTime);
      const clock = `${stamp(position)} / ${duration ? stamp(duration) : '--:--'}`;
      if (time.textContent !== clock) time.textContent = clock;
      button.querySelector('[data-play-icon]').toggleAttribute('hidden', !audio.paused && !audio.ended);
      button.querySelector('[data-pause-icon]').toggleAttribute('hidden', audio.paused || audio.ended);
      const label = `${audio.paused || audio.ended ? 'Воспроизвести' : 'Приостановить'} ${audio.getAttribute('aria-label').toLowerCase()}`;
      if (button.getAttribute('aria-label') !== label) button.setAttribute('aria-label', label);
      button.disabled = pending || Boolean(audio.error);
    }

    function tick() {
      frame = 0;
      if (!root.isConnected) return;
      draw();
      if (!audio.paused && !audio.ended && !audio.error) frame = requestAnimationFrame(tick);
    }

    function sync() {
      cancelAnimationFrame(frame);
      frame = 0;
      draw();
      if (!audio.paused && !audio.ended && !audio.error) frame = requestAnimationFrame(tick);
    }

    listen(button, 'click', async () => {
      if (!audio.paused && !audio.ended) {
        audio.pause();
        return;
      }
      pending = true;
      error.hidden = true;
      draw();
      try {
        await audio.play();
      } catch (failure) {
        if (root.isConnected && failure?.name !== 'AbortError') {
          error.textContent = 'Не удалось воспроизвести аудио.';
          error.hidden = false;
        }
      } finally {
        pending = false;
        if (root.isConnected) sync();
      }
    });
    listen(seek, 'input', () => {
      if (seek.disabled) return;
      audio.currentTime = Math.min(Number(seek.value), audio.duration);
      draw();
    });
    for (const name of ['loadedmetadata', 'durationchange', 'timeupdate', 'play', 'playing', 'pause', 'ended', 'seeked', 'emptied']) {
      listen(audio, name, sync);
    }
    listen(audio, 'waiting', () => button.setAttribute('aria-busy', 'true'));
    for (const name of ['playing', 'pause', 'ended', 'error']) {
      listen(audio, name, () => button.removeAttribute('aria-busy'));
    }
    function mediaError() {
      error.textContent = 'Не удалось загрузить аудио.';
      error.hidden = false;
      sync();
    }
    listen(audio, 'error', mediaError);
    if (audio.error) mediaError();
    else sync();
    controls.hidden = false;
    root.classList.add('is-enhanced');
    audio.controls = false;
    players.set(root, () => {
      cancelAnimationFrame(frame);
      events.abort();
      audio.pause();
    });
  }

  function refresh() {
    for (const [root, dispose] of players) {
      if (!root.isConnected) {
        dispose();
        players.delete(root);
      }
    }
    for (const root of document.querySelectorAll('.review-player')) {
      if (!players.has(root)) mount(root);
    }
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', refresh, {once: true});
  else refresh();
  for (const name of ['htmx:load', 'htmx:afterSettle']) document.addEventListener(name, refresh);
  window.addEventListener('pagehide', () => {
    for (const root of players.keys()) root.querySelector('audio').pause();
  });
})();
