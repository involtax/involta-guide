import { z } from "zod";
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
  name: z.string().trim().min(1).max(200),
  cuisine: z.string().trim().min(1).max(300),
  description: z.string().trim().min(1).max(2000),
  mapsUrl,
  sources: z.array(url).max(20).default([]),
});
export const contentSchema = z.object({
  intro: z.string().max(4000),
  restaurants: z.array(restaurant).max(10),
});
export const generatedSchema = contentSchema.extend({
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
