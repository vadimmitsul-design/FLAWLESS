import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "FLAWLESS — твоё AI-пространство",
  description: "Модели, диалоги и расходы в одном рабочем пространстве.",
  robots: { index: false, follow: false },
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="ru">
      <body>{children}</body>
    </html>
  );
}
