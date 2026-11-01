import { NextResponse } from "next/server";
import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { isAdmin } from "@/lib/auth";
import { one, root } from "@/lib/db.mjs";
export async function GET(
  _request: Request,
  { params }: { params: Promise<{ id: string }> },
) {
  if (!(await isAdmin())) return new Response("Unauthorized", { status: 401 });
  const { id } = await params;
  const r = one("SELECT audio,mime FROM reviews WHERE id=?", id);
  if (!r) return new Response("Not found", { status: 404 });
  try {
    return new NextResponse(await readFile(join(root, "audio", r.audio)), {
      headers: {
        "Content-Type": r.mime,
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
      },
    });
  } catch {
    return new Response("Not found", { status: 404 });
  }
}
