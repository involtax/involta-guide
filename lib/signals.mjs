export function similarity(a, b) {
  const tokenize = (s) =>
    new Set(
      (s || "")
        .toLowerCase()
        .replace(/[^\p{L}\p{N}\s]/gu, " ")
        .split(/\s+/)
        .filter((w) => w.length > 2),
    );
  const x = tokenize(a),
    y = tokenize(b);
  if (!x.size || !y.size) return 0;
  const intersection = [...x].filter((w) => y.has(w)).length;
  return intersection / new Set([...x, ...y]).size;
}
export function reviewSignals(review, others) {
  return others
    .map((r) => ({
      id: r.id,
      city_id: r.city_id,
      created_at: r.created_at,
      moderation: r.moderation,
      similarity: similarity(review.transcript, r.transcript),
      sameAudio: r.hash === review.hash,
      sameVisitor: r.visitor === review.visitor,
      sameIp: r.ip_hash === review.ip_hash && review.ip_hash !== "",
    }))
    .filter(
      (r) => r.sameAudio || r.sameVisitor || r.sameIp || r.similarity >= 0.45,
    )
    .sort(
      (a, b) =>
        Number(b.sameAudio) - Number(a.sameAudio) ||
        b.similarity - a.similarity,
    )
    .slice(0, 20);
}
