import { FACTORS, FACTOR_LABELS } from "@/lib/ranking.mjs";
export default function RankingReport({ report }: { report: any[] }) {
  if (!report.length) return null;
  return (
    <details>
      <summary>
        Оценки кандидатов · {report.length} · восемь равных весов
      </summary>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Ресторан</th>
              {FACTORS.map((k) => (
                <th key={k}>
                  {FACTOR_LABELS[k as keyof typeof FACTOR_LABELS]}
                </th>
              ))}
              <th>Итог</th>
            </tr>
          </thead>
          <tbody>
            {report.map((r) => (
              <tr key={r.name}>
                <td>{r.name}</td>
                {FACTORS.map((k) => (
                  <td key={k}>{r.scores[k] ?? "—"}</td>
                ))}
                <td>
                  <strong>{r.totalScore?.toFixed(2) ?? "Нет данных"}</strong>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {report.map((r) => (
        <details key={r.name}>
          <summary>{r.name} · основания оценок</summary>
          {r.evidence.map((e: any) => (
            <p key={e.factor}>
              <strong>
                {FACTOR_LABELS[e.factor as keyof typeof FACTOR_LABELS]}
              </strong>
              : {e.reason}
              <small>
                Уверенность: {e.confidence}.{" "}
                {e.sources.map((s: string) => (
                  <a key={s} href={s} target="_blank" rel="noreferrer">
                    Источник ↗{" "}
                  </a>
                ))}
              </small>
            </p>
          ))}
        </details>
      ))}
    </details>
  );
}
