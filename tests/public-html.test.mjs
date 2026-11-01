import test from "node:test";
import assert from "node:assert/strict";
import { documentHtml, restaurantHtml, jsonHtml } from "../lib/public-html.mjs";
test("публичный HTML не содержит React или гидратацию и экранирует данные ресторана", () => {
  const card = restaurantHtml(
    {
      name: "<script>alert(1)</script>",
      cuisine: '" onmouseover="bad',
      description: "<img src=x onerror=bad>",
      mapsUrl: "javascript:alert(1)",
      phone: "+74951234567",
    },
    0,
    null,
    "city",
  );
  const html = documentHtml({ body: card });
  assert.doesNotMatch(
    html,
    /<script>alert|<img src=x|href="javascript:|__next_f|\/_next\//,
  );
  assert.match(html, /&lt;script&gt;/);
  assert.match(html, /tel:\+74951234567/);
  assert.equal(
    JSON.parse(jsonHtml({ text: "</script><script>bad</script>" })).text,
    "</script><script>bad</script>",
  );
  assert.doesNotMatch(jsonHtml({ text: "</script>" }), /<\/script>/);
});
