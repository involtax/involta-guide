import {
  ancestors,
  childLocations,
  availableRestaurants,
  locationHeading,
  LOCATION_KINDS,
} from "./locations.mjs";
export async function generationPrompt(location, template) {
  const [parents, children, restaurants] = await Promise.all([
    ancestors(location.id),
    childLocations(location.id, { includeHidden: true }),
    availableRestaurants(location.id),
  ]);
  const scope =
    location.coverage_note ||
    `Только ${location.name}, ${location.region}. Не включать соседние территории без явного указания.`;
  const substitutions = {
    "{локация}": location.name,
    "{город}": location.name,
    "{регион}": location.region,
    "{лимит}": String(location.top_limit),
    "{родитель}": parents.at(-1)?.name || "нет",
    "{охват}": scope,
  };
  let prompt = template;
  for (const [key, value] of Object.entries(substitutions))
    prompt = prompt.replaceAll(key, value);
  return (
    prompt +
    `\n\nОБЯЗАТЕЛЬНЫЙ ГЕОГРАФИЧЕСКИЙ КОНТЕКСТ\n${JSON.stringify({ location: location.name, type: LOCATION_KINDS[location.kind], heading: locationHeading(location), administrativeRegion: location.region, editorialParents: parents.map((p) => p.name), scope, coordinates: { latitude: location.latitude, longitude: location.longitude }, bounds: location.bounds ? JSON.parse(location.bounds) : null, isHub: location.is_hub, childLocations: children.map((c) => ({ name: c.name, kind: c.kind, region: c.region })), existingRestaurants: restaurants.slice(0, 100).map((r) => ({ id: r.id, name: r.name, location: r.location_name, mapsUrl: r.maps_url })), topLimit: location.top_limit })}\n«Город» в старых формулировках промпта означает текущую локацию: это может быть район, посёлок, курорт или направление. Редакционный родитель не означает административную принадлежность. Для направления можно отбирать заведения дочерних локаций; для конкретного района/курорта нельзя подменять его соседними местами. Не выдумывай радиус, границы или координаты. Проверяй принадлежность каждого заведения текущему охвату. Если используешь ресторан из existingRestaurants, верни его существующий id. Не создавай дубликат. Население не определяет размер топа: лимит задан редактором. При нехватке подходящих мест верни меньше, вплоть до нуля. Газпром Поляна — одна локация, Лаура и Альпика не отдельные страницы.`
  );
}
