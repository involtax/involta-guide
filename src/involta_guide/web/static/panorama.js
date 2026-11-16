(() => {
  const panorama = document.querySelector("[data-panorama]");
  if (!panorama) return;
  const scroller = panorama.querySelector(".panorama-scroller");
  const image = scroller.querySelector(".panorama-image");
  const track = scroller.querySelector(".panorama-track");
  const reduced = matchMedia("(prefers-reduced-motion: reduce)");
  const mobile = matchMedia("(max-width: 650px)");
  const sizeViewport = () =>
    document.documentElement.style.setProperty(
      "--panorama-viewport",
      document.documentElement.clientWidth + "px",
    );
  sizeViewport();
  panorama.dataset.panoramaEnhanced = "";
  let horizontalDrag = false,
    frame = 0,
    previous = 0,
    position = 0,
    direction = -1;
  let resumeAt = 0,
    timer = 0,
    measuredWidth = 0;
  let gesture = null,
    touchActive = false,
    elastic = 0,
    bounce = null,
    wheelTimer = 0,
    returnTimer = 0,
    returnFrame = 0;
  const clamp = (value, low, high) => Math.max(low, Math.min(high, value));
  const cityEnd = () => Math.max(0, image.offsetWidth - scroller.clientWidth);
  const fullEnd = () => Math.max(0, track.offsetWidth - scroller.clientWidth);
  function autoBounds() {
    const end = cityEnd(),
      inset = image.offsetWidth * (mobile.matches ? 0.12 : 0.1);
    const low = Math.min(inset, end / 2);
    return { low, high: Math.max(low, end - inset) };
  }
  // Focus and vertical scrolling do not interrupt the decorative motion.
  const allowed = () =>
    mobile.matches &&
    scroller.scrollLeft <= cityEnd() &&
    !horizontalDrag &&
    !returnFrame &&
    !touchActive &&
    !bounce &&
    !document.hidden &&
    !reduced.matches;
  function stopAuto() {
    cancelAnimationFrame(frame);
    frame = 0;
    previous = 0;
  }
  function startAuto() {
    if (!frame && allowed()) frame = requestAnimationFrame(tick);
  }
  function refresh() {
    if (allowed()) startAuto();
    else stopAuto();
  }
  function stopReturn() {
    clearTimeout(returnTimer);
    cancelAnimationFrame(returnFrame);
    returnFrame = 0;
  }
  function scheduleReturn() {
    clearTimeout(returnTimer);
    if (scroller.scrollLeft <= cityEnd() || gesture || touchActive || bounce ||
        returnFrame || document.hidden || reduced.matches) return;
    returnTimer = setTimeout(() => {
      if (gesture || touchActive || bounce || document.hidden || reduced.matches) return;
      stopAuto();
      const from = scroller.scrollLeft, target = autoBounds().high;
      const started = performance.now();
      function returnToCity(now) {
        const progress = Math.min(1, (now - started) / 900);
        // Smooth acceleration and braking, independent of the distance travelled.
        const eased = progress * progress * (3 - 2 * progress);
        scroller.scrollLeft = from + (target - from) * eased;
        if (progress < 1) returnFrame = requestAnimationFrame(returnToCity);
        else {
          returnFrame = 0;
          direction = -1;
          resumeLater();
        }
      }
      returnFrame = requestAnimationFrame(returnToCity);
    }, 1000);
  }
  function resumeLater() {
    stopAuto();
    resumeAt = performance.now() + 1800;
    clearTimeout(timer);
    timer = setTimeout(startAuto, 1800);
    scheduleReturn();
  }
  function clearBounce() {
    if (bounce) {
      bounce.onfinish = null;
      bounce.cancel();
      bounce = null;
    }
  }
  function rubber(raw) {
    clearBounce();
    elastic =
      Math.sign(raw) * 66 * (1 - Math.exp((-Math.abs(raw) * 0.48) / 66));
    track.style.transform = `translate3d(${elastic}px,0,0)`;
  }
  function settle() {
    clearTimeout(wheelTimer);
    clearBounce();
    const from = elastic;
    elastic = 0;
    track.style.transform = "";
    if (Math.abs(from) < 0.5 || reduced.matches) {
      resumeLater();
      return;
    }
    bounce = track.animate(
      [
        { transform: `translate3d(${from}px,0,0)`, offset: 0 },
        { transform: `translate3d(${-from * 0.07}px,0,0)`, offset: 0.78 },
        { transform: "translate3d(0,0,0)", offset: 1 },
      ],
      { duration: 440, easing: "cubic-bezier(.16,1,.3,1)" },
    );
    bounce.onfinish = () => {
      bounce = null;
      resumeLater();
    };
  }
  function measure() {
    if (!image.complete || !image.naturalWidth || !scroller.clientWidth) {
      stopAuto();
      return;
    }
    if (measuredWidth !== scroller.clientWidth) {
      stopReturn();
      clearBounce();
      elastic = 0;
      track.style.transform = "";
      scroller.scrollLeft = autoBounds().high;
      direction = -1;
      measuredWidth = scroller.clientWidth;
    }
    position = scroller.scrollLeft;
    scroller.tabIndex = 0;
    refresh();
  }
  function tick(now) {
    frame = 0;
    if (!allowed()) return;
    if (now < resumeAt) {
      clearTimeout(timer);
      timer = setTimeout(startAuto, resumeAt - now);
      return;
    }
    const { low, high } = autoBounds();
    if (high <= low) return;
    if (!previous) {
      previous = now;
      position = clamp(scroller.scrollLeft, 0, fullEnd());
      if (position < low) direction = 1;
      if (position > high) direction = -1;
    }
    const dt = Math.min((now - previous) / 1000, 0.05);
    previous = now;
    const inside = position >= low && position <= high;
    const ease = inside
      ? Math.max(
          0.18,
          Math.min(1, (position - low) / 55, (high - position) / 55),
        )
      : 1;
    const before = position;
    position = clamp(position + direction * 14 * ease * dt, 0, fullEnd());
    if (direction > 0 && before <= high && position >= high) {
      position = high;
      direction = -1;
    }
    if (direction < 0 && before >= low && position <= low) {
      position = low;
      direction = 1;
    }
    scroller.scrollLeft = position;
    frame = requestAnimationFrame(tick);
  }
  // Let the browser own touch scrolling, axis selection and momentum.
  // pointercancel is normal when Safari takes over a native scroll gesture.
  scroller.addEventListener("touchstart", () => {
    touchActive = true;
    stopAuto();
    stopReturn();
    clearBounce();
    elastic = 0;
    track.style.transform = "";
  }, { passive: true });
  function releaseTouch(event) {
    touchActive = event.touches.length > 0;
    if (!touchActive) resumeLater();
  }
  scroller.addEventListener("touchend", releaseTouch, { passive: true });
  scroller.addEventListener("touchcancel", releaseTouch, { passive: true });
  scroller.addEventListener("pointerdown", (event) => {
    if (event.pointerType === "touch" || !event.isPrimary || event.button !== 0 || !fullEnd()) return;
    stopReturn();
    gesture = {
      id: event.pointerId,
      x: event.clientX,
      y: event.clientY,
      scroll: scroller.scrollLeft,
      axis: "",
    };
  });
  scroller.addEventListener("pointermove", (event) => {
    if (!gesture || event.pointerId !== gesture.id) return;
    const dx = event.clientX - gesture.x,
      dy = event.clientY - gesture.y;
    if (!gesture.axis && Math.max(Math.abs(dx), Math.abs(dy)) > 7) {
      gesture.axis = Math.abs(dx) > Math.abs(dy) ? "x" : "y";
      if (gesture.axis === "x") {
        horizontalDrag = true;
        stopAuto();
        clearBounce();
        scroller.setPointerCapture(event.pointerId);
        gesture.scroll = scroller.scrollLeft;
      }
    }
    if (gesture.axis !== "x") return;
    if (event.cancelable) event.preventDefault();
    const desired = gesture.scroll - dx,
      bounded = clamp(desired, 0, fullEnd());
    scroller.scrollLeft = bounded;
    rubber(bounded - desired);
  });
  function release(event) {
    if (!gesture || event.pointerId !== gesture.id) return;
    const done = gesture;
    gesture = null;
    if (done.axis !== "x") {
      scheduleReturn();
      return;
    }
    horizontalDrag = false;
    if (Math.abs(elastic) > 0.5) settle();
    else resumeLater();
  }
  window.addEventListener("pointerup", release);
  scroller.addEventListener("pointercancel", release);
  scroller.addEventListener("lostpointercapture", (event) => {
    if (
      event.target === scroller &&
      !scroller.hasPointerCapture(event.pointerId)
    )
      release(event);
  });
  scroller.addEventListener(
    "wheel",
    (event) => {
      stopReturn();
      scheduleReturn();
      if (!fullEnd()) return;
      const dx = event.shiftKey && !event.deltaX ? event.deltaY : event.deltaX;
      if (!dx || (!event.shiftKey && Math.abs(dx) <= Math.abs(event.deltaY)))
        return;
      if (event.cancelable) event.preventDefault();
      stopAuto();
      const delta =
        dx *
        (event.deltaMode === 1
          ? 16
          : event.deltaMode === 2
            ? scroller.clientWidth
            : 1);
      const desired = scroller.scrollLeft + delta,
        bounded = clamp(desired, 0, fullEnd());
      scroller.scrollLeft = bounded;
      if (desired !== bounded) {
        rubber(bounded - desired + elastic * 1.5);
        clearTimeout(wheelTimer);
        wheelTimer = setTimeout(settle, 100);
      } else {
        rubber(0);
        resumeLater();
      }
    },
    { passive: false },
  );
  scroller.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    stopReturn();
    stopAuto();
    clearBounce();
    elastic = 0;
    track.style.transform = "";
    const target =
      event.key === "Home"
        ? 0
        : event.key === "End"
          ? fullEnd()
          : scroller.scrollLeft + (event.key === "ArrowLeft" ? -60 : 60);
    scroller.scrollLeft = clamp(target, 0, fullEnd());
    resumeLater();
  });
  reduced.addEventListener("change", () => {
    if (reduced.matches) {
      stopReturn();
      clearBounce();
      elastic = 0;
      track.style.transform = "";
    }
    refresh();
    scheduleReturn();
  });
  mobile.addEventListener("change", () => {
    measuredWidth = 0;
    stopAuto();
    measure();
  });
  window.addEventListener("resize", sizeViewport);
  scroller.addEventListener("scroll", () => {
    // Native momentum continues after touchend. Every scroll postpones both
    // autoplay and the secret return; our own animation frames must not do so.
    if (!frame && !returnFrame) resumeLater();
  }, { passive: true });
  window.addEventListener("scroll", () => {
    stopReturn();
    scheduleReturn();
  }, { passive: true });
  document.addEventListener("visibilitychange", () => {
    stopReturn();
    refresh();
    scheduleReturn();
  });
  new ResizeObserver(measure).observe(scroller);
  image.addEventListener("load", measure);
  if (image.complete) measure();
})();
