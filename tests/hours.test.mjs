import test from "node:test";
import assert from "node:assert/strict";
import { openingStatus } from "../lib/hours.mjs";
import { hoursSchema, phoneSchema } from "../lib/schema.mjs";
const hours = {
  timezone: "Europe/Moscow",
  week: [[["12:00", "02:00"]], [], [], [], [["12:00", "23:00"]], [], []],
};
test("расписание использует часовой пояс и ночное продолжение предыдущего дня", () => {
  assert.equal(
    openingStatus(hours, new Date("2026-11-02T10:00Z")),
    "Открыто до 02:00",
  );
  assert.equal(
    openingStatus(hours, new Date("2026-11-02T22:00Z")),
    "Открыто до 02:00",
  );
  assert.equal(
    openingStatus(hours, new Date("2026-11-02T23:00Z")),
    "Откроется 06.11 в 12:00",
  );
});
test("исключения, несколько интервалов, неизвестные часы и валидация", () => {
  const h = {
    ...hours,
    exceptions: {
      "2026-11-02": [
        ["15:00", "18:00"],
        ["19:00", "22:00"],
      ],
    },
  };
  assert.equal(
    openingStatus(h, new Date("2026-11-02T15:30Z")),
    "Откроется сегодня в 19:00",
  );
  assert.match(openingStatus(null), /уточняется/);
  assert.throws(() => hoursSchema.parse({ ...hours, timezone: "Made/Up" }));
  assert.throws(() => phoneSchema.parse("123"));
  assert.equal(phoneSchema.parse("+78121234567"), "+78121234567");
});
