import { openingStatus } from "./hours-core.js";
const elements = [...document.querySelectorAll("[data-hours]")].map((el) => ({
  el,
  hours: JSON.parse(el.dataset.hours),
}));
function update() {
  for (const { el, hours } of elements)
    el.querySelector("summary").textContent = openingStatus(hours);
}
update();
const timer = setInterval(update, 30000);
window.addEventListener("pagehide", () => clearInterval(timer), { once: true });
