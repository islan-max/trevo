"use client";

import { useEffect } from "react";
import { AlertTriangle, Repeat } from "@/components/icons";

export default function Error({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  useEffect(() => {
    console.error(error);
  }, [error]);

  return (
    <div className="flex min-h-[70vh] items-center justify-center p-4">
      <div className="app-card w-full max-w-md p-6 text-center shadow-soft">
        <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-app border border-line bg-danger/10 text-danger">
          <AlertTriangle aria-hidden size={22} />
        </div>
        <h1 className="mt-4 text-lg font-bold text-ink">Algo não saiu como o esperado</h1>
        <p className="mt-2 text-sm text-muted">
          Tivemos um problema ao carregar esta página. Tente novamente — se persistir, volte para o início.
        </p>
        <div className="mt-5 flex flex-wrap justify-center gap-2">
          <button className="btn-primary" onClick={reset} type="button">
            <Repeat aria-hidden size={16} />
            Tentar novamente
          </button>
          {/* <a> em vez de next/link: um erro de render pode ter deixado o
              router num estado ruim, então voltar ao início aqui é
              propositalmente uma navegação completa (recarrega a página do
              zero), não uma troca de rota client-side. */}
          {/* eslint-disable-next-line @next/next/no-html-link-for-pages */}
          <a className="btn-secondary" href="/">
            Voltar ao início
          </a>
        </div>
      </div>
    </div>
  );
}
