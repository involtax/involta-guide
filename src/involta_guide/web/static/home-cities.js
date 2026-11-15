// Progressive enhancement: without canvas/JS, city names are ordinary links.
(() => {
  const motion = matchMedia('(prefers-reduced-motion: reduce)');
  const masks = [];
  let frame;
  let lastDraw = 0;

  function draw(time = 0) {
    const seconds = motion.matches ? 0 : time / 1000;
    for (const mask of masks) {
      if (!mask.link.hasAttribute('data-concealed') || !mask.visible) continue;
      const { canvas, context, particles } = mask;
      context.clearRect(0, 0, canvas.clientWidth, canvas.clientHeight);
      for (const p of particles) {
        context.beginPath();
        context.arc(
          p.x + Math.sin(seconds * p.speed + p.phase) * 1.7,
          p.y + Math.cos(seconds * p.speed * .8 + p.phase) * 1.2,
          p.radius, 0, Math.PI * 2,
        );
        context.fillStyle = `rgba(21,21,21,${p.opacity * (.72 + .28 * Math.sin(seconds * 1.5 + p.phase))})`;
        context.fill();
      }
    }
  }
  function animate(time) {
    if (time - lastDraw > 40) { draw(time); lastDraw = time; }
    frame = requestAnimationFrame(animate);
  }
  function resume() {
    cancelAnimationFrame(frame);
    draw();
    if (!document.hidden && !motion.matches && masks.some(m => m.visible && m.link.hasAttribute('data-concealed'))) {
      frame = requestAnimationFrame(animate);
    }
  }
  function resize() {
    for (const mask of masks) {
      if (!mask.link.hasAttribute('data-concealed')) continue;
      const { canvas, context } = mask;
      const width = canvas.clientWidth, height = canvas.clientHeight;
      const ratio = Math.min(devicePixelRatio || 1, 2);
      canvas.width = Math.round(width * ratio);
      canvas.height = Math.round(height * ratio);
      context.setTransform(ratio, 0, 0, ratio, 0, 0);
      mask.particles = Array.from({ length: Math.round(width * 4) }, () => ({
        x: 3 + Math.random() * (width - 6), y: 4 + Math.random() * (height - 8),
        radius: .45 + Math.random() * .7, opacity: .12 + Math.random() * .35,
        phase: Math.random() * Math.PI * 2, speed: .4 + Math.random() * .8,
      }));
    }
    resume();
  }
  for (const link of document.querySelectorAll('.home-city-name')) {
    const canvas = link.querySelector('canvas');
    const context = canvas.getContext('2d');
    if (!context) continue;
    const name = link.querySelector('span');
    link.setAttribute('data-concealed', '');
    link.setAttribute('aria-label', 'Узнать город');
    name.setAttribute('aria-hidden', 'true');
    masks.push({ link, canvas, context, particles: [], visible: true });
    link.addEventListener('click', event => {
      // Preserve open-in-new-tab and other native link gestures.
      if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      if (!link.hasAttribute('data-concealed')) return;
      event.preventDefault();
      link.removeAttribute('data-concealed');
      link.removeAttribute('aria-label');
      name.removeAttribute('aria-hidden');
      resume();
    });
  }
  if (!masks.length) return;
  const observer = new IntersectionObserver(entries => {
    for (const entry of entries) {
      masks.find(mask => mask.link === entry.target).visible = entry.isIntersecting;
    }
    resume();
  });
  masks.forEach(mask => observer.observe(mask.link));
  window.addEventListener('resize', resize);
  document.addEventListener('visibilitychange', resume);
  motion.addEventListener('change', resume);
  resize();
})();
