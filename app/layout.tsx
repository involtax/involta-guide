import { siteUrl } from "@/lib/urls.mjs";
import type { Metadata } from "next";
import Link from "next/link";
import "./style.css";
export const metadata: Metadata = {
  metadataBase: new URL(siteUrl() + "/"),
  title: {
    default: "Инвольта гид — где поесть в России",
    template: "%s · Инвольта гид",
  },
  description:
    "Короткие подборки ресторанов в городах, районах и на курортах России. Редакционный выбор и голосовые отзывы гостей.",
};
export default function Layout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="ru">
      <body>
        <header className="header">
          <Link href="/" className="brand">
            инвольта<span>гид</span>
            <sup>Россия</sup>
          </Link>
          <Link href="/" className="header-note">
            Места, ради которых стоит зайти.
          </Link>
        </header>
        {children}
        <footer>
          <Link href="/">Инвольта гид</Link>
          <span>Небольшие подборки. Личный опыт.</span>
          <Link href="/admin">Редакция ↗</Link>
        </footer>
      </body>
    </html>
  );
}
