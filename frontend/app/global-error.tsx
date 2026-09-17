"use client";

import { useEffect } from "react";

// Só entra em cena quando o próprio layout raiz falha — substitui <html> e
// <body> inteiros, então não pode depender de ThemeProvider, das fontes locais
// nem das classes utilitárias de globals.css: usa cores da marca em estilo
// inline para continuar com a identidade Trevo mesmo nesse cenário extremo.
export default function GlobalError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  useEffect(() => {
    console.error(error);
  }, [error]);

  return (
    <html lang="pt-BR">
      <body
        style={{
          alignItems: "center",
          background: "#F4F8F4",
          color: "#17251C",
          display: "flex",
          fontFamily: "ui-sans-serif, system-ui, sans-serif",
          justifyContent: "center",
          margin: 0,
          minHeight: "100vh",
          padding: 16
        }}
      >
        <div
          style={{
            background: "#FFFFFF",
            border: "1px solid #C2D6C6",
            borderRadius: 16,
            boxShadow: "0 12px 32px rgba(16, 40, 26, 0.12)",
            maxWidth: 420,
            padding: 32,
            textAlign: "center",
            width: "100%"
          }}
        >
          <h1 style={{ fontSize: 18, fontWeight: 700, margin: 0 }}>O Trevo travou de vez</h1>
          <p style={{ color: "#5C7062", fontSize: 14, marginTop: 8 }}>
            Um erro grave impediu a página de carregar. Tente novamente — se continuar, recarregue a página.
          </p>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8, justifyContent: "center", marginTop: 20 }}>
            <button
              onClick={reset}
              style={{
                background: "linear-gradient(180deg, #2E9D5B, #19653B)",
                border: "none",
                borderRadius: 10,
                color: "#FFFFFF",
                cursor: "pointer",
                fontSize: 14,
                fontWeight: 600,
                padding: "10px 18px"
              }}
              type="button"
            >
              Tentar novamente
            </button>
            {/* <a> em vez de next/link: o layout raiz falhou, então o router
                do App Router pode não ter contexto utilizável aqui — só uma
                navegação completa é confiável neste fallback. */}
            {/* eslint-disable-next-line @next/next/no-html-link-for-pages */}
            <a
              href="/"
              style={{
                background: "transparent",
                border: "1px solid #C2D6C6",
                borderRadius: 10,
                color: "#17251C",
                fontSize: 14,
                fontWeight: 600,
                padding: "10px 18px",
                textDecoration: "none"
              }}
            >
              Voltar ao início
            </a>
          </div>
        </div>
      </body>
    </html>
  );
}
