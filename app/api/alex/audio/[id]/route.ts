import { cookies } from "next/headers";
import { NextResponse } from "next/server";
import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { alexSession } from "@/lib/alex.mjs";
import { isAdmin } from "@/lib/auth";
import { one, root } from "@/lib/db.mjs";
export async function GET(
  _request: Request,
  { params }: { params: Promise<{ id: string }> },
) {
  const session = await alexSession((await cookies()).get("guide_alex")?.value);
  if (!session && !(await isAdmin()))
    return new Response("Unauthorized", { status: 401 });
  const { id } = await params;
  if (!/^[a-f0-9-]{36}$/.test(id))
    return new Response("Not found", { status: 404 });
  const review = await one(
    "SELECT audio,mime FROM reviews WHERE id=$1 AND alex_session_id IS NOT NULL",
    id,
  );
  if (!review) return new Response("Not found", { status: 404 });
  try {
    return new NextResponse(await readFile(join(root, "audio", review.audio)), {
      headers: {
        "Content-Type": review.mime,
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
      },
    });
  } catch {
    return new Response("Not found", { status: 404 });
  }
}
