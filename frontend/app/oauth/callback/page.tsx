"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { LoaderCircle } from "@/components/icons";
import { AuthBrand } from "@/components/auth/AuthBrand";
import { rememberSession } from "@/lib/authSession";

function OAuthCallbackContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [message, setMessage] = useState("Finalizando login social...");

  useEffect(() => {
    const hashParams = new URLSearchParams(window.location.hash.replace(/^#/, ""));
    const token = searchParams.get("access_token") || hashParams.get("access_token");
    const hasCookieSession = searchParams.get("session") === "1";
    const linked = searchParams.get("linked") === "1";
    const error = searchParams.get("error");

    if (linked) {
      // Vínculo de conta social feito a partir de uma sessão já autenticada
      // (perfil) — volta para lá em vez de /dashboard.
      window.history.replaceState(null, "", "/oauth/callback");
      router.replace("/perfil?linked=1");
      return;
    }

    if (token || hasCookieSession) {
      rememberSession();
      window.history.replaceState(null, "", "/oauth/callback");
      router.replace("/dashboard");
      return;
    }

    if (error) {
      setMessage(decodeURIComponent(error));
      return;
    }

    setMessage("Resposta OAuth inválida. Tente novamente.");
  }, [router, searchParams]);

  const hasError = Boolean(searchParams.get("error"));

  return (
    <main className="auth-glow flex min-h-screen items-center justify-center px-4 py-10">
      <div className="glass-panel w-full max-w-md p-6 text-center">
        <AuthBrand />
        <div className="mt-6 flex flex-col items-center gap-3">
          {!hasError ? <LoaderCircle className="animate-spin text-leaf" size={28} /> : null}
          <p className={`text-sm ${hasError ? "text-danger" : "text-muted"}`}>{message}</p>
          {hasError ? (
            <Link className="btn-primary mt-2" href="/login">
              Voltar para login
            </Link>
          ) : null}
        </div>
      </div>
    </main>
  );
}

export default function OAuthCallbackPage() {
  return (
    <Suspense
      fallback={
        <main className="auth-glow flex min-h-screen items-center justify-center px-4">
          <p className="text-sm text-muted">Carregando...</p>
        </main>
      }
    >
      <OAuthCallbackContent />
    </Suspense>
  );
}
