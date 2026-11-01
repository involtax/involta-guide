import { NextResponse } from "next/server";
import { requireAdmin, checkOrigin } from "@/lib/auth";
import { processJob } from "@/lib/ai.server";
export async function POST(request: Request) {
  try {
    await checkOrigin(request);
    await requireAdmin();
    const processed = await processJob();
    return NextResponse.json({ processed });
  } catch {
    return NextResponse.json(
      { error: "Нет доступа или обработчик недоступен" },
      { status: 403 },
    );
  }
}
