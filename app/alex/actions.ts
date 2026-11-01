"use server";
import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { revalidatePath } from "next/cache";
import { requireAdmin } from "@/lib/auth";
import { alexSession } from "@/lib/alex.mjs";
import { run, audit } from "@/lib/db.mjs";
export async function revokeAlexSessions() {
  await requireAdmin();
  await run("UPDATE alex_sessions SET revoked=true WHERE NOT revoked");
  await audit("alex_revoke", "all", "Администратор завершил все сессии Алекса");
  revalidatePath("/alex");
}
export async function alexLogout() {
  const jar = await cookies();
  const session = await alexSession(jar.get("guide_alex")?.value);
  if (session)
    await run("UPDATE alex_sessions SET revoked=true WHERE id=$1", session.id);
  jar.delete("guide_alex");
  redirect("/guide");
}
