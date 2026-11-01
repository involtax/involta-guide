import test from "node:test";
import assert from "node:assert/strict";
import { testDatabase } from "./database.mjs";
import {
  all,
  one,
  run,
  id,
  closeDb,
  saveDraft,
  publish,
  locationContent,
  setting,
  setSetting,
} from "../lib/db.mjs";
import { migrate, seedLocations } from "../lib/migrate.mjs";
import {
  locationBySlug,
  locationHeading,
  ancestors,
  descendants,
  childLocations,
  searchLocations,
  saveLocation,
  availableRestaurants,
  linkRestaurant,
  hasPublishedDescendants,
} from "../lib/locations.mjs";
import { generationPrompt } from "../lib/generation-context.mjs";
const input = (name, extra = {}) => ({
  name,
  region: "Тестовая область",
  slug: "test-" + name.toLowerCase(),
  kind: "settlement",
  parentId: null,
  isHub: false,
  inPhrase: "в тестовой локации",
  aliases: [],
  priority: "medium",
  topLimit: 3,
  active: true,
  listed: true,
  population: null,
  populationVerified: false,
  latitude: null,
  longitude: null,
  coverageNote: "",
  bounds: null,
  relatedIds: [],
  ...extra,
});
const card = {
  name: "Тестовый ресторан",
  phone: "+74951234567",
  cuisine: "Русская",
  description: "Опубликованное описание.",
  mapsUrl: "https://yandex.ru/maps/org/test/123/",
  sources: [],
};
test("локации: миграция, импорт, иерархия и публикация", async (t) => {
  await closeDb();
  const testDb = await testDatabase();
  try {
    await t.test(
      "миграция сохраняет старые ID, ресторан, отзыв, URL и версию страницы",
      async () => {
        await migrate({ through: "001_initial.sql" });
        const locationId = id(),
          restaurantId = id(),
          versionId = id(),
          reviewId = id();
        await run(
          "INSERT INTO cities(id,name,region,slug) VALUES($1,$2,$3,$4)",
          locationId,
          "Город миграции",
          "Тестовая область",
          "legacy-location",
        );
        await run(
          "INSERT INTO restaurants(id,city_id,name,cuisine,description,maps_url) VALUES($1,$2,$3,$4,$5,$6)",
          restaurantId,
          locationId,
          card.name,
          card.cuisine,
          card.description,
          card.mapsUrl,
        );
        await run(
          "INSERT INTO versions(id,city_id,number,content,source) VALUES($1,$2,1,$3,$4)",
          versionId,
          locationId,
          JSON.stringify({
            intro: "Старый текст",
            restaurants: [{ ...card, id: restaurantId }],
          }),
          "manual",
        );
        await run(
          "UPDATE cities SET published_version=$1,draft_version=$1 WHERE id=$2",
          versionId,
          locationId,
        );
        await run(
          "INSERT INTO reviews(id,city_id,visitor,audio,mime,hash,ip_hash,device) VALUES($1,$2,$3,$4,$5,$6,$7,$8)",
          reviewId,
          locationId,
          "visitor",
          "keep.webm",
          "audio/webm",
          "hash",
          "ip",
          "browser",
        );
        await migrate();
        assert.equal(
          (
            await one(
              "SELECT published_version FROM locations WHERE id=$1",
              locationId,
            )
          ).published_version,
          versionId,
        );
        assert.equal(
          (await one("SELECT location_id FROM reviews WHERE id=$1", reviewId))
            .location_id,
          locationId,
        );
        assert.equal(
          (await locationBySlug("legacy-location")).location.id,
          locationId,
        );
        assert.equal((await locationContent(locationId)).intro, "Старый текст");
        assert.ok(
          await one(
            "SELECT restaurant_id FROM restaurant_locations WHERE restaurant_id=$1",
            restaurantId,
          ),
        );
      },
    );
    await seedLocations();
    await t.test(
      "Газпром одна локация; Хаджох — дополнительное название Каменномостского",
      async () => {
        const gas = (
          await searchLocations({
            query: "газпром альпика",
            publicOnly: true,
            status: "active",
          })
        ).rows;
        assert.equal(gas.length, 1);
        assert.equal(gas[0].name, "Газпром Поляна");
        assert.equal(
          (
            await all(
              "SELECT id FROM locations WHERE name IN ('Газпром Лаура','Газпром Альпика','Хаджох')",
            )
          ).length,
          0,
        );
        assert.equal(
          (await locationBySlug("gazprom-laura")).location.id,
          gas[0].id,
        );
        assert.equal(
          (await searchLocations({ query: "Хаджох" })).rows[0].name,
          "Каменномостский",
        );
      },
    );
    await t.test(
      "Сочи и Красная Поляна имеют разные охваты, Сириус сохраняет отдельный регион",
      async () => {
        const sochi = await one("SELECT * FROM locations WHERE slug='sochi'");
        const sirius = await one("SELECT * FROM locations WHERE slug='sirius'");
        assert.equal(sirius.parent_id, sochi.id);
        assert.equal(sirius.region, "Федеральная территория Сириус");
        assert.equal(locationHeading(sirius), "Где поесть в Сириусе");
        const roza = await one(
          "SELECT * FROM locations WHERE slug='roza-khutor'",
        );
        assert.equal(locationHeading(roza), "Где поесть на Розе Хутор");
        assert.deepEqual(
          (await ancestors(roza.id)).map((l) => l.name),
          ["Сочи", "Красная Поляна", "Эсто-Садок"],
        );
        assert.ok(
          await one("SELECT id FROM locations WHERE slug='sochi-centre'"),
        );
        assert.ok(
          await one(
            "SELECT id FROM locations WHERE slug='krasnaya-polyana-poselok'",
          ),
        );
        assert.ok(
          await one(
            "SELECT id FROM locations WHERE slug='krasnaya-polyana-kurort'",
          ),
        );
      },
    );
    await t.test(
      "в Москве 132 административных района и 20 публичных гастрономических зон",
      async () => {
        const moscow = (await locationBySlug("moskva")).location;
        assert.equal(
          (
            await one(
              "SELECT count(*) n FROM locations WHERE region='Москва' AND kind='district'",
            )
          ).n,
          132,
        );
        assert.equal((await childLocations(moscow.id)).length, 20);
        assert.ok(
          (await childLocations(moscow.id, { includeHidden: true })).length >
            132,
        );
        assert.equal(
          (
            await searchLocations({
              query: "Капотня",
              publicOnly: true,
              status: "active",
            })
          ).total,
          0,
        );
        assert.equal((await searchLocations({ query: "Капотня" })).total, 1);
        assert.equal(
          (
            await searchLocations({
              query: "патрики",
              publicOnly: true,
              status: "active",
            })
          ).rows[0].name,
          "Патриаршие пруды",
        );
      },
    );
    await t.test(
      "неподтверждённая численность территории не используется как население или лимит",
      async () => {
        const roza = (await locationBySlug("roza-khutor")).location;
        assert.equal(roza.population, null);
        assert.equal(roza.population_basis, "service_area_proxy");
        assert.equal(roza.top_limit, 3);
        const gas = (await locationBySlug("gazprom-polyana")).location;
        assert.equal(gas.top_limit, 2);
      },
    );
    await t.test(
      "повтор импорта сохраняет ручные поля и текущий промпт",
      async () => {
        const sirius = (await locationBySlug("sirius")).location;
        await run("UPDATE locations SET top_limit=4 WHERE id=$1", sirius.id);
        await setSetting("prompt", "Ручной промпт редактора");
        assert.equal(await seedLocations(), 0);
        assert.equal(
          (await one("SELECT top_limit FROM locations WHERE id=$1", sirius.id))
            .top_limit,
          4,
        );
        assert.equal(await setting("prompt"), "Ручной промпт редактора");
      },
    );
    let parent, child, other;
    await t.test("редактор создаёт иерархию и блокирует циклы", async () => {
      parent = await saveLocation(null, input("Parent", { isHub: true }));
      child = await saveLocation(null, input("Child", { parentId: parent }));
      other = await saveLocation(null, input("Other"));
      await assert.rejects(
        () => saveLocation(parent, input("Parent", { parentId: child })),
        /цикл/,
      );
      assert.equal((await descendants(parent)).length, 2);
      await assert.rejects(
        () => saveLocation(parent, input("Parent", { active: false })),
        /дочерние/,
      );
    });
    await t.test(
      "смена URL создаёт постоянный алиас и защищает его от захвата",
      async () => {
        await saveLocation(other, input("Other", { slug: "test-renamed" }));
        const old = await locationBySlug("test-other");
        assert.equal(old.redirect, true);
        assert.equal(old.location.slug, "test-renamed");
        await assert.rejects(
          () => saveLocation(null, input("Hijack", { slug: "test-other" })),
          /URL/,
        );
      },
    );
    await t.test(
      "направление использует опубликованный ресторан без копии и без утечки черновика",
      async () => {
        const version = await saveDraft(child, {
          intro: "Дочерняя страница",
          restaurants: [card],
        });
        await publish(child, version);
        const content = await locationContent(child);
        const restaurant = content.restaurants[0];
        await saveDraft(child, {
          ...content,
          restaurants: [
            { ...restaurant, description: "НЕОПУБЛИКОВАННЫЙ ТЕКСТ" },
          ],
        });
        const choices = await availableRestaurants(parent);
        assert.equal(choices[0].description, card.description);
        const parentVersion = await saveDraft(parent, {
          intro: "Направление",
          restaurants: [
            { ...restaurant, description: "Редакционный текст направления" },
          ],
        });
        assert.equal(
          (
            await one(
              "SELECT count(*) n FROM restaurants WHERE id=$1",
              restaurant.id,
            )
          ).n,
          1,
        );
        assert.equal(
          (await locationContent(parent)).restaurants[0].locationId,
          child,
        );
        assert.equal(
          (
            await one(
              "SELECT location_id FROM restaurants WHERE id=$1",
              restaurant.id,
            )
          ).location_id,
          child,
        );
        assert.equal(
          (
            await one(
              "SELECT description FROM restaurants WHERE id=$1",
              restaurant.id,
            )
          ).description,
          "НЕОПУБЛИКОВАННЫЙ ТЕКСТ",
        );
        assert.equal(
          (
            await one("SELECT content FROM versions WHERE id=$1", version)
          ).content.includes("НЕОПУБЛИКОВАННЫЙ"),
          false,
        );
        assert.equal(
          (
            await one(
              "SELECT published_version FROM locations WHERE id=$1",
              parent,
            )
          ).published_version,
          null,
        );
        await publish(parent, parentVersion);
        assert.equal(await hasPublishedDescendants(parent), true);
        await assert.rejects(
          () =>
            saveDraft(other, {
              intro: "Другое место",
              restaurants: [restaurant],
            }),
          /свяжите/,
        );
        await linkRestaurant(restaurant.id, other);
        await saveDraft(other, {
          intro: "Связанное место",
          restaurants: [restaurant],
        });
      },
    );
    await t.test(
      "промпт получает локацию, явный лимит и географический охват",
      async () => {
        const gas = (await locationBySlug("gazprom-polyana")).location;
        const prompt = await generationPrompt(
          gas,
          "Локация {локация}; старое имя {город}; лимит {лимит}; {охват}",
        );
        assert.ok(prompt.includes("лимит 2"));
        assert.ok(!prompt.includes("{город}"));
        assert.ok(prompt.includes("Лаура и Альпика не отдельные страницы"));
        assert.ok(prompt.includes("Эсто-Садок"));
        assert.ok(prompt.includes("Газпром Поляна"));
      },
    );
    await t.test("пагинация и поиск не размножают результаты", async () => {
      const page1 = await searchLocations({ page: 1, pageSize: 10 }),
        page2 = await searchLocations({ page: 2, pageSize: 10 });
      assert.equal(page1.rows.length, 10);
      assert.equal(page2.rows.length, 10);
      assert.equal(
        page1.rows.some((a) => page2.rows.some((b) => a.id === b.id)),
        false,
      );
    });
  } finally {
    await closeDb();
    await testDb.cleanup();
  }
});
