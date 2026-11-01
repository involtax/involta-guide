import { COOKIE_PATH } from "@/lib/urls.mjs";
import { NextResponse } from "next/server";
import { cookies } from "next/headers";
import { createHash, createHmac, randomUUID } from "node:crypto";
import { writeFile, unlink } from "node:fs/promises";
import { join } from "node:path";
import { one, run, id, root, transaction } from "@/lib/db.mjs";
import { checkOrigin } from "@/lib/auth";
import { alexSession } from "@/lib/alex.mjs";
export const runtime = "nodejs";
export async function POST(request: Request) {
  let path: string | undefined;
  try {
    await checkOrigin(request);
    if (Number(request.headers.get("content-length") || 0) > 11 * 1024 * 1024)
      return NextResponse.json(
        { error: "Запись слишком большая (до 10 МБ)" },
        { status: 413 },
      );
    if (!request.body) throw new Error("Нет записи");
    const reader = request.body.getReader();
    const chunks: Uint8Array[] = [];
    let total = 0;
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > 11 * 1024 * 1024) {
        await reader.cancel();
        return NextResponse.json(
          { error: "Запись слишком большая (до 10 МБ)" },
          { status: 413 },
        );
      }
      chunks.push(value);
    }
    const bounded = new Request(request.url, {
      method: "POST",
      headers: { "content-type": request.headers.get("content-type") || "" },
      body: Buffer.concat(chunks),
    });
    const form = await bounded.formData();
    const audio = form.get("audio");
    const locationId = String(
      form.get("locationId") || form.get("cityId") || "",
    );
    const duration = Number(form.get("duration"));
    if (form.get("consent") !== "yes") throw new Error("Необходимо согласие");
    if (
      !(await one(
        "SELECT id FROM locations WHERE id=$1 AND active=1",
        locationId,
      ))
    )
      throw new Error("Локация не найдена");
    if (
      !(audio instanceof File) ||
      audio.size < 100 ||
      audio.size > 10 * 1024 * 1024 ||
      !Number.isFinite(duration) ||
      duration < 1 ||
      duration > 121
    )
      throw new Error("Нужна запись от 1 до 120 секунд, до 10 МБ");
    const mime = audio.type.split(";")[0];
    const extensions: Record<string, string> = {
      "audio/webm": "webm",
      "audio/mp4": "mp4",
      "audio/ogg": "ogg",
      "audio/wav": "wav",
    };
    if (!extensions[mime]) throw new Error("Неподдерживаемый формат записи");
    const bytes = Buffer.from(await audio.arrayBuffer());
    const valid =
      (mime === "audio/webm" &&
        bytes.subarray(0, 4).equals(Buffer.from([0x1a, 0x45, 0xdf, 0xa3]))) ||
      (mime === "audio/ogg" && bytes.subarray(0, 4).toString() === "OggS") ||
      (mime === "audio/mp4" && bytes.subarray(4, 8).toString() === "ftyp") ||
      (mime === "audio/wav" && bytes.subarray(0, 4).toString() === "RIFF");
    if (!valid) throw new Error("Файл не похож на аудиозапись");
    const jar = await cookies();
    const alex = await alexSession(jar.get("guide_alex")?.value);
    let visitor = jar.get("guide_visitor")?.value;
    if (!visitor || !/^[a-f0-9-]{36}$/.test(visitor)) visitor = randomUUID();
    const reviewId = id();
    const filename = `${reviewId}.${extensions[mime]}`;
    path = join(root, "audio", filename);
    await writeFile(path, bytes, { flag: "wx" });
    const hash = createHash("sha256").update(bytes).digest("hex");
    const ipHash = createHmac("sha256", process.env.SESSION_SECRET || "local")
      .update(
        request.headers.get("x-forwarded-for")?.split(",")[0]?.trim() ||
          "unknown",
      )
      .digest("hex");
    await transaction(async () => {
      await run(
        "INSERT INTO reviews(id,location_id,visitor,audio,mime,hash,ip_hash,device) VALUES($1,$2,$3,$4,$5,$6,$7,$8)",
        reviewId,
        locationId,
        visitor,
        filename,
        mime,
        hash,
        ipHash,
        (request.headers.get("user-agent") || "").slice(0, 500),
      );
      if (alex) {
        await run(
          "UPDATE reviews SET alex_session_id=$1 WHERE id=$2",
          alex.id,
          reviewId,
        );
        await run(
          "INSERT INTO alex_changes(id,session_id,review_id,location_id,status,detail) VALUES($1,$2,$3,$4,'queued','Ожидает распознавания')",
          id(),
          alex.id,
          reviewId,
          locationId,
        );
      }
      await run(
        "INSERT INTO jobs(id,kind,location_id,review_id) VALUES($1,'review',$2,$3)",
        id(),
        locationId,
        reviewId,
      );
    });
    jar.set("guide_visitor", visitor, {
      httpOnly: true,
      sameSite: "lax",
      secure: new URL(request.url).protocol === "https:",
      maxAge: 365 * 24 * 60 * 60,
      path: COOKIE_PATH,
    });
    return NextResponse.json({ id: reviewId }, { status: 201 });
  } catch (e) {
    if (path) await unlink(path).catch(() => {});
    return NextResponse.json(
      { error: e instanceof Error ? e.message : "Не удалось сохранить запись" },
      { status: 400 },
    );
  }
}
