import type { Metadata, Viewport } from "next";
import localFont from "next/font/local";
import { RootShell } from "@/components/RootShell";
import { ThemeProvider, ThemeScript } from "@/lib/theme";
import "./globals.css";

// Fontes servidas pelo próprio domínio: a CSP da API declara `font-src 'self'`,
// então buscar do Google Fonts em runtime seria bloqueado. Os .woff2 variáveis
// estão versionados em app/fonts.
//
// PERF-06: só o subset "latin" é usado — o app é pt-BR only (lang="pt-BR"
// abaixo) e todo caractere do português (incluindo ç, ã, õ) já está no
// Latin-1 Supplement, coberto por esse subset. O subset "latin-ext" (Extended-A,
// usado por polonês/tcheco/romeno) foi removido: as duas entradas de src
// tinham o mesmo unicode-range implícito (next/font/local não expõe
// unicode-range por src), então o subsetting não tinha efeito nenhum — e o
// latin-ext media 85 kB/35 kB (Inter/Nunito) que o pt-BR nunca precisava.
const display = localFont({
  src: [{ path: "./fonts/Inter-latin.woff2", style: "normal" }],
  variable: "--font-display",
  display: "swap",
  weight: "100 900",
  fallback: ["ui-sans-serif", "system-ui", "sans-serif"]
});

const body = localFont({
  src: [{ path: "./fonts/Nunito-latin.woff2", style: "normal" }],
  variable: "--font-body",
  display: "swap",
  weight: "200 1000",
  fallback: ["ui-sans-serif", "system-ui", "sans-serif"]
});

export const metadata: Metadata = {
  title: "Trevo — suas finanças com sorte e método",
  description: "Acompanhe salário, despesas, metas, orçamento e parcelas em um só lugar.",
  icons: {
    icon: "/favicon.svg",
    apple: "/apple-touch-icon.svg"
  },
  manifest: "/manifest.webmanifest"
};

export const viewport: Viewport = {
  themeColor: "#1F8049"
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html className={`${display.variable} ${body.variable}`} lang="pt-BR" suppressHydrationWarning>
      <body className="font-sans">
        <ThemeScript />
        <ThemeProvider>
          <RootShell>{children}</RootShell>
        </ThemeProvider>
      </body>
    </html>
  );
}
