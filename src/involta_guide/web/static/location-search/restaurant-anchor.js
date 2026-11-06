(() => {
  function reveal() {
    if (!/^#restaurant-[0-9a-f-]{36}$/i.test(location.hash)) return;
    const card = document.getElementById(location.hash.slice(1));
    if (!card) return;
    const more = card.closest('details');
    if (more && !more.open) {
      more.open = true;
      requestAnimationFrame(() => card.scrollIntoView({block: 'start'}));
    }
  }
  window.addEventListener('hashchange', reveal);
  reveal();
})();
