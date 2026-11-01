"use client";
import { useEffect, useState } from "react";
import { DAYS, openingStatus } from "@/lib/hours.mjs";
export default function RestaurantHours({ hours }: { hours: any }) {
  const [now, setNow] = useState<Date | null>(null);
  useEffect(() => {
    setNow(new Date());
    const timer = setInterval(() => setNow(new Date()), 30000);
    return () => clearInterval(timer);
  }, []);
  if (!hours) return <p className="muted">Режим работы уточняется</p>;
  return (
    <details>
      <summary>{now ? openingStatus(hours, now) : "Режим работы"}</summary>
      <ul>
        {hours.week.map((slots: string[][], i: number) => (
          <li key={i}>
            {DAYS[i]}:{" "}
            {slots.length
              ? slots.map((s) => s.join("–")).join(", ")
              : "выходной"}
          </li>
        ))}
      </ul>
      {Object.entries(hours.exceptions || {}).map(
        ([date, slots]: [string, any]) => (
          <p key={date}>
            {date}:{" "}
            {slots.length
              ? slots.map((s: string[]) => s.join("–")).join(", ")
              : "выходной"}
          </p>
        ),
      )}
      <small>Часовой пояс: {hours.timezone}</small>
    </details>
  );
}
