import { publicPath, siteUrl } from "@/lib/urls.mjs";
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
          <a href={publicPath("/")} className="brand">
            инвольта<span>гид</span>
            <sup>Россия</sup>
          </a>
          <a href={publicPath("/")} className="header-note">
            Места, ради которых стоит зайти.
          </a>
        </header>
        {children}
        <footer>
          <a href={publicPath("/")}>Инвольта гид</a>
          <span>Небольшие подборки. Личный опыт.</span>
          <Link href="/admin">Редакция ↗</Link>
        </footer>
      </body>
    </html>
  );
}
