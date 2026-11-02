export const DAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];
export function openingStatus(hours, now = new Date()) {
  if (!hours) return "Режим работы уточняется";
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-GB", {
      timeZone: hours.timezone,
      weekday: "short",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
    })
      .formatToParts(now)
      .map((p) => [p.type, p.value]),
  );
  const today = `${parts.year}-${parts.month}-${parts.day}`;
  const day = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].indexOf(
    parts.weekday,
  );
  const minute = Number(parts.hour) * 60 + Number(parts.minute);
  const clock = (t) => Number(t.slice(0, 2)) * 60 + Number(t.slice(3));
  const dateAt = (offset) => {
    const d = new Date(today + "T12:00:00Z");
    d.setUTCDate(d.getUTCDate() + offset);
    return d.toISOString().slice(0, 10);
  };
  const slotsAt = (offset) =>
    hours.exceptions?.[dateAt(offset)] ?? hours.week[(day + offset + 7) % 7];
  for (const slot of slotsAt(-1)) {
    if (clock(slot[1]) <= clock(slot[0]) && minute < clock(slot[1]))
      return `Открыто до ${slot[1]}`;
  }
  for (const slot of slotsAt(0)) {
    const end =
      clock(slot[1]) <= clock(slot[0]) ? clock(slot[1]) + 1440 : clock(slot[1]);
    if (minute >= clock(slot[0]) && minute < end)
      return `Открыто до ${slot[1]}`;
  }
  for (let offset = 0; offset < 14; offset++) {
    const next = [...slotsAt(offset)]
      .sort((a, b) => clock(a[0]) - clock(b[0]))
      .find((s) => offset > 0 || clock(s[0]) > minute);
    if (next)
      return `Откроется ${offset === 0 ? "сегодня" : offset === 1 ? "завтра" : dateAt(offset).split("-").reverse().slice(0, 2).join(".")} в ${next[0]}`;
  }
  return "Закрыто · уточните расписание";
}
