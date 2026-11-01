"use client";
import { publicPath } from "@/lib/urls.mjs";
import { useState } from "react";
import { useRouter } from "next/navigation";
export default function QueueRunner() {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const router = useRouter();
  async function run() {
    setBusy(true);
    setMessage("");
    try {
      const r = await fetch(publicPath("/api/admin/process"), {
        method: "POST",
      });
      const data = await r.json();
      if (!r.ok) throw new Error(data.error);
      setMessage(
        data.processed
          ? "Задание завершено. Проверьте результат в очереди."
          : "Очередь пуста.",
      );
      router.refresh();
    } catch (e) {
      setMessage(e instanceof Error ? e.message : "Ошибка запроса");
    } finally {
      setBusy(false);
    }
  }
  return (
    <div>
      <button onClick={run} disabled={busy}>
        {busy ? "Обрабатываем…" : "Выполнить одно задание ↗"}
      </button>
      {message && <p role="status">{message}</p>}
    </div>
  );
}
