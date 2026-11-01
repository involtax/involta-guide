import { publicPath, absoluteUrl } from "./urls.mjs";
import { DAYS, openingStatus } from "./hours.mjs";
export const escapeHtml = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
export const jsonHtml = (value) =>
  JSON.stringify(value)
    .replace(/</g, "\\u003c")
    .replace(/>/g, "\\u003e")
    .replace(/&/g, "\\u0026");
const e = escapeHtml;
export const link = (path, label, attrs = "") =>
  `<a href="${e(publicPath(path))}" ${attrs}>${label}</a>`;
export function documentHtml({
  title = "Инвольта гид — где поесть в России",
  description = "Короткие подборки ресторанов в городах, районах и на курортах России.",
  path = "/",
  index = true,
  body,
  scripts = "",
  structured = [],
}) {
  return `<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${e(title)}</title><meta name="description" content="${e(description)}"><meta name="robots" content="${index ? "index" : "noindex"}, follow"><link rel="canonical" href="${e(absoluteUrl(path))}"><link rel="stylesheet" href="${e(publicPath("/assets/style.css"))}"></head><body><header class="header">${link("/", "инвольта<span>гид</span><sup>Россия</sup>", 'class="brand"')}${link("/", "Места, ради которых стоит зайти.", 'class="header-note"')}</header><main>${body}</main><footer>${link("/", "Инвольта гид")}<span>Небольшие подборки. Личный опыт.</span>${link("/admin", "Редакция ↗")}</footer>${structured.length ? `<script type="application/ld+json">${jsonHtml(structured)}</script>` : ""}${scripts}</body></html>`;
}
export function hoursHtml(hours) {
  if (!hours) return '<p class="muted">Режим работы уточняется</p>';
  return `<details data-hours="${e(JSON.stringify(hours))}"><summary>${e(openingStatus(hours))}</summary><ul>${hours.week.map((slots, i) => `<li>${DAYS[i]}: ${e(slots.length ? slots.map((s) => s.join("–")).join(", ") : "выходной")}</li>`).join("")}</ul>${Object.entries(
    hours.exceptions || {},
  )
    .map(
      ([date, slots]) =>
        `<p>${e(date)}: ${e(slots.length ? slots.map((s) => s.join("–")).join(", ") : "выходной")}</p>`,
    )
    .join("")}<small>Часовой пояс: ${e(hours.timezone)}</small></details>`;
}
export function restaurantHtml(r, i, home, currentId) {
  const maps = /^https:\/\//.test(r.mapsUrl || "") ? r.mapsUrl : "";
  return `<article class="restaurant"><span class="rank">${String(i + 1).padStart(2, "0")}</span><div><p class="eyebrow">${e(r.cuisine)}${home && home.id !== currentId ? " · " + link("/" + home.slug, e(home.name)) : ""}</p><h2>${e(r.name)} ${r.alexStar ? '<span title="Выбор Алекса" aria-label="Выбор Алекса">★ </span>' : ""}<a href="${e(maps)}" target="_blank" rel="noopener noreferrer" aria-label="${e(r.name)} — Яндекс Карты" title="Яндекс Карты">↗</a></h2><p>${e(r.description)}</p>${r.phone ? `<p><a href="tel:${e(r.phone)}">${e(r.phone)}</a></p>` : ""}${hoursHtml(r.openingHours)}</div></article>`;
}
export function voiceHtml(location, alex) {
  return `<section class="voice-section" id="review" data-voice-widget data-location-id="${e(location.id)}" data-api-base="${e(publicPath("/api"))}"><div><p class="eyebrow">ВАШ ГОЛОС В ГИДЕ</p><h2>Были здесь?<br><span class="serif">Расскажите.</span></h2><p>Хороший ужин, неудачный опыт или место, которого не хватает в подборке ${e(location.name)}? Нажмите кнопку и расскажите голосом. Редакция услышит.</p><small>${alex ? "Команда применяется после распознавания." : "До 2 минут. Без регистрации. Отзыв появится в обработке, а изменения в гиде проверит редактор."}</small></div><div class="recorder"><label class="consent"><input type="checkbox" data-consent> Согласен на хранение аудио и обработку отзыва, включая расшифровку через OpenAI. Не сообщайте личные данные других людей.</label><div data-controls><button class="record-button" data-start>● Записать отзыв</button></div><p role="alert" class="error" data-error hidden></p><noscript>Для записи голоса включите JavaScript. Справочник доступен без него.</noscript></div><div class="my-reviews" data-history hidden></div></section>`;
}
