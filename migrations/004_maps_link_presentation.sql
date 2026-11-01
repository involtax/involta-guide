-- Preserve the editable prompt and append the presentation rule to existing installations.
UPDATE settings
SET value = value || E'\n\n' || $rule$Ссылка на карту отображается только значком ↗ справа от названия ресторана: «Название ↗», где значок — ссылка на mapsUrl. Не добавляй отдельную строку или видимую подпись «На Яндекс Картах», «Карты» и подобные. В JSON сохраняй name без значка и разметки, а mapsUrl — отдельным полем: значок добавляет интерфейс. При текстовом показе результата используй «Название [↗](mapsUrl)».$rule$
WHERE key = 'prompt'
  AND position('Ссылка на карту отображается только значком' in value) = 0;
