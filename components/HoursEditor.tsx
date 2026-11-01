"use client";
import { DAYS } from "@/lib/hours.mjs";
export default function HoursEditor({
  value,
  onChange,
}: {
  value: any;
  onChange: (v: any) => void;
}) {
  if (!value)
    return (
      <button
        type="button"
        className="secondary"
        onClick={() =>
          onChange({
            timezone: "Europe/Moscow",
            week: Array.from({ length: 7 }, () => []),
          })
        }
      >
        Добавить режим работы
      </button>
    );
  return (
    <fieldset>
      <legend>Режим работы</legend>
      <label>
        Часовой пояс
        <input
          value={value.timezone}
          onChange={(e) => onChange({ ...value, timezone: e.target.value })}
          placeholder="Europe/Moscow"
        />
      </label>
      {DAYS.map((day: string, i: number) => (
        <label key={day}>
          {day}
          <input
            key={JSON.stringify(value.week[i])}
            defaultValue={value.week[i]
              .map((s: string[]) => s.join("–"))
              .join(", ")}
            placeholder="12:00–23:00 или пусто — выходной"
            onBlur={(e) => {
              const parts = e.target.value.trim()
                ? e.target.value.split(",").map((t) =>
                    t
                      .trim()
                      .split(/[–-]/)
                      .map((v) => v.trim()),
                  )
                : [];
              if (
                parts.some(
                  (p) =>
                    p.length !== 2 ||
                    p.some((t) => !/^([01]\d|2[0-3]):[0-5]\d$/.test(t)),
                )
              ) {
                e.target.setCustomValidity("Пример: 12:00–15:00, 18:00–02:00");
                e.target.reportValidity();
                return;
              }
              e.target.setCustomValidity("");
              onChange({
                ...value,
                week: value.week.map((s: any, n: number) =>
                  n === i ? parts : s,
                ),
              });
            }}
          />
        </label>
      ))}
      <label>
        Исключения по датам (JSON)
        <textarea
          defaultValue={JSON.stringify(value.exceptions || {})}
          placeholder={'{"2026-12-31":[]}'}
          onBlur={(e) => {
            try {
              const exceptions = JSON.parse(e.target.value || "{}");
              e.target.setCustomValidity("");
              onChange({ ...value, exceptions });
            } catch {
              e.target.setCustomValidity("Проверьте JSON исключений");
              e.target.reportValidity();
            }
          }}
        />
      </label>
      <button
        type="button"
        className="secondary"
        onClick={() => onChange(null)}
      >
        Режим неизвестен
      </button>
    </fieldset>
  );
}
