"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { FormEvent, useEffect, useState } from "react";
import { ArrowRight, Moon, Sun } from "@/components/icons";
import { api } from "@/lib/api";
import { COOKIE_AUTH_TOKEN, rememberSession } from "@/lib/authSession";
import { useTheme } from "@/lib/theme";
import { AuthBenefits } from "@/components/auth/AuthBenefits";
import { AuthBrand } from "@/components/auth/AuthBrand";
import { AuthProductDemo } from "@/components/auth/AuthProductDemo";
import { SocialLoginButtons } from "@/components/auth/SocialLoginButtons";

export default function LoginPage() {
  const router = useRouter();
  const { effectiveTheme, preference, setPreference } = useTheme();
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  useEffect(() => {
    let active = true;
    api
      .me(COOKIE_AUTH_TOKEN)
      .then(() => {
        if (!active) return;
        rememberSession();
        router.replace("/dashboard");
      })
      .catch(() => undefined);
    return () => {
      active = false;
    };
  }, [router]);

  useEffect(() => {
    // UX-03: chega aqui logo após trocar a senha em /perfil, quando o token
    // anterior já foi invalidado pelo backend — sem isto, o motivo do
    // redirecionamento para /login se perdia.
    if (new URLSearchParams(window.location.search).get("reason") === "password-changed") {
      setNotice("Senha atualizada. Faça login novamente para continuar.");
      window.history.replaceState(null, "", "/login");
    }
  }, []);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    try {
      await api.login(String(form.get("email")), String(form.get("password")));
      rememberSession();
      router.replace("/dashboard");
    } catch (err) {
      setNotice("");
      setError(err instanceof Error ? err.message : "Falha ao entrar.");
    }
  }

  function toggleTheme() {
    setPreference(effectiveTheme === "dark" ? "light" : "dark");
  }

  return (
    <main className="auth-glow relative min-h-screen px-4 py-6 lg:px-10 lg:py-10">
      <button
        type="button"
        className="btn-secondary absolute right-4 top-4 z-10 px-3 py-2 lg:right-10"
        onClick={toggleTheme}
        aria-label="Alternar tema"
      >
        {preference === "system" ? (
          effectiveTheme === "dark" ? <Moon size={16} /> : <Sun size={16} />
        ) : effectiveTheme === "dark" ? (
          <Moon size={16} />
        ) : (
          <Sun size={16} />
        )}
      </button>

      <div className="glass-panel mx-auto grid w-full max-w-6xl overflow-hidden lg:grid-cols-[1.05fr_0.95fr]">
        <section className="order-2 border-t border-line/70 p-5 lg:order-1 lg:border-r lg:border-t-0 lg:p-7">
          <AuthBrand />
          <p className="mt-5 max-w-xl text-sm text-muted">
            Controle salário, despesas, metas e parcelas em um só lugar. Veja o que importa antes de entrar.
          </p>
          <div className="mt-6 hidden lg:block">
            <AuthProductDemo />
          </div>
          <div className="mt-4 lg:hidden">
            <AuthProductDemo compact />
          </div>
          <div className="mt-5">
            <AuthBenefits compact />
          </div>
        </section>

        <section className="order-1 mx-auto w-full max-w-md p-5 lg:order-2 lg:p-7">
          <form onSubmit={handleSubmit}>
            <p className="text-sm font-bold text-leaf">Entre no Trevo</p>
            <h1 className="mt-1 text-2xl font-black leading-tight text-ink">Seu mês inteiro em uma tela</h1>
            <p className="mt-2 text-sm text-muted">Saiba quanto pode gastar hoje e feche o mês no azul.</p>

            <label className="mt-5 block text-sm font-semibold text-ink">
              E-mail
              <input className="field mt-1" name="email" type="email" autoComplete="email" required />
            </label>
            <label className="mt-3 block text-sm font-semibold text-ink">
              Senha
              <input className="field mt-1" name="password" type="password" autoComplete="current-password" required />
            </label>

            {notice ? (
              <p aria-live="polite" className="mt-3 rounded-app bg-success/10 p-3 text-sm text-success">
                {notice}
              </p>
            ) : null}
            {error ? (
              <p aria-live="polite" className="mt-3 rounded-app bg-danger/10 p-3 text-sm text-danger">
                {error}
              </p>
            ) : null}

            <button className="btn-primary mt-5 w-full" type="submit">
              Entrar
              <ArrowRight size={16} />
            </button>

            <SocialLoginButtons mode="login" />

            <Link className="mt-4 block text-center text-sm font-bold text-leaf-700" href="/cadastro">
              Criar conta gratuita
            </Link>
          </form>

        </section>
      </div>
    </main>
  );
}
