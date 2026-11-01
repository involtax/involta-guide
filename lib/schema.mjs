import { z } from "zod";
import { FACTORS } from "./ranking.mjs";
const url = z
  .string()
  .url()
  .max(2000)
  .refine((s) => {
    const u = new URL(s);
    return u.protocol === "https:" && !u.username && !u.password;
  }, "Нужна HTTPS-ссылка");
export const mapsUrl = url.refine((s) => {
  const h = new URL(s).hostname;
  return (
    ["yandex.ru", "yandex.com", "yandex.by", "yandex.kz", "ya.ru"].includes(
      h,
    ) || h.endsWith(".yandex.ru")
  );
}, "Нужна ссылка на Яндекс Карты");
export const restaurant = z.object({
  id: z.string().uuid().optional(),
  locationId: z.string().uuid().optional(),
  locationName: z.string().max(200).optional(),
  latitude: z.number().min(-90).max(90).nullable().optional(),
  longitude: z.number().min(-180).max(180).nullable().optional(),
  name: z.string().trim().min(1).max(200),
  cuisine: z.string().trim().min(1).max(300),
  description: z.string().trim().min(1).max(2000),
  mapsUrl,
  sources: z.array(url).max(20).default([]),
});
const factorScore = z.number().int().min(0).max(10).nullable();
export const rankingCandidate = z.object({
  name: z.string().trim().min(1).max(200),
  mapsUrl: mapsUrl.nullable(),
  scores: z.object(
    Object.fromEntries(FACTORS.map((key) => [key, factorScore])),
  ),
  evidence: z
    .array(
      z.object({
        factor: z.enum(FACTORS),
        reason: z.string().min(1).max(2000),
        confidence: z.enum(["high", "medium", "low"]),
        sources: z.array(url).max(20),
      }),
    )
    .length(8)
    .refine(
      (items) => new Set(items.map((e) => e.factor)).size === 8,
      "Нужно обоснование каждого из восьми факторов",
    ),
  totalScore: z.number().min(0).max(10).nullable(),
});
export const contentSchema = z.object({
  intro: z.string().max(4000),
  restaurants: z.array(restaurant).max(10),
  rankingReport: z.array(rankingCandidate).max(10).optional(),
});
export const generatedSchema = contentSchema.extend({
  rankingReport: z.array(rankingCandidate).max(10),
  warnings: z.array(z.string().max(2000)).max(20),
});
export const rating = z.number().min(0).max(10).nullable();
export const signal = z.number().min(-10).max(10).nullable();
export const reviewSchema = z
  .object({
    entries: z
      .array(
        z.object({
          restaurantId: z.string().nullable(),
          restaurantName: z.string().max(200),
          type: z.enum(["review", "rating"]),
          text: z.string().max(4000).nullable(),
          quote: z.string().max(4000),
          sentiment: signal,
          recommendation: signal,
          rating,
          confidence: z.number().min(0).max(1),
          topics: z.array(
            z.enum([
              "food",
              "service",
              "atmosphere",
              "cleanliness",
              "price",
              "facts",
            ]),
          ),
          experience: z.enum(["personal", "hearsay", "unknown"]),
        }),
      )
      .min(1)
      .max(20),
    suggestions: z
      .array(
        z.object({
          kind: z.enum(["edit", "restaurant"]),
          restaurantId: z.string().nullable(),
          name: z.string().max(200),
          cuisine: z.string().max(300),
          description: z.string().max(2000),
          mapsUrl: z.string().max(2000),
          reason: z.string().max(2000),
        }),
      )
      .max(20),
  })
  .superRefine((v, ctx) => {
    v.entries.forEach((e, i) => {
      if (e.type === "review" && !e.text?.trim())
        ctx.addIssue({
          code: "custom",
          path: ["entries", i, "text"],
          message: "Текст обязателен для отзыва",
        });
      if (
        e.type === "rating" &&
        e.rating === null &&
        e.sentiment === null &&
        e.recommendation === null
      )
        ctx.addIssue({
          code: "custom",
          path: ["entries", i],
          message: "Оценка должна содержать хотя бы один показатель",
        });
    });
  });
