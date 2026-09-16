"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { ChangeEvent, FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { Camera, Download, Mail, ShieldAlert, Trash2, Upload, UserRound, WalletCards } from "@/components/icons";
import { FeedbackMessage } from "@/components/FeedbackMessage";
import { KpiCard } from "@/components/KpiCard";
import { PageHeader } from "@/components/PageHeader";
import { SectionIntro } from "@/components/SectionIntro";
import { Shell } from "@/components/Shell";
import { api, apiAssetUrl, type OAuthProviderKey, type OAuthProvidersResponse } from "@/lib/api";
import { COOKIE_AUTH_TOKEN, clearSession } from "@/lib/authSession";
import { formatBRL } from "@/lib/format";
import { useAuthToken } from "@/lib/useAuthToken";
import type { Bootstrap, User } from "@/types/finance";

type ProfileForm = {
  name: string;
};

const PHOTO_MAX_BYTES = 512 * 1024;
const PHOTO_TYPES = ["image/jpeg", "image/png", "image/webp"];

const OAUTH_PROVIDER_LABELS: Record<OAuthProviderKey, string> = {
  google: "Google",
  github: "GitHub",
  facebook: "Facebook"
};

function initialsFromUser(user: User | null) {
  const source = user?.name || user?.email || "Usuario";
  const parts = source.trim().split(/\s+/).filter(Boolean);
  if (parts.length >= 2) return `${parts[0][0]}${parts[1][0]}`.toUpperCase();
  return source.slice(0, 2).toUpperCase();
}

function ProfilePhoto({ initials, name, preview }: { initials: string; name: string; preview: string }) {
  if (preview) {
    return (
      <span
        aria-label={`Foto de perfil de ${name}`}
        className="block h-24 w-24 rounded-app bg-cover bg-center shadow-soft ring-1 ring-line"
        role="img"
        style={{ backgroundImage: `url(${preview})` }}
      />
    );
  }

  return (
    <span className="flex h-24 w-24 items-center justify-center rounded-app bg-gradient-to-br from-leaf to-leaf-700 text-2xl font-black text-white shadow-soft" aria-hidden>
      {initials}
    </span>
  );
}

export default function PerfilPage() {
  const token = useAuthToken();
  const router = useRouter();
  const [user, setUser] = useState<User | null>(null);
  const [boot, setBoot] = useState<Bootstrap | null>(null);
  const [profile, setProfile] = useState<ProfileForm>({ name: "" });
  const [photoFile, setPhotoFile] = useState<File | null>(null);
  const [photoPreview, setPhotoPreview] = useState("");
  const [message, setMessage] = useState("");
  const [savingProfile, setSavingProfile] = useState(false);
  const [deletePassword, setDeletePassword] = useState("");
  const [deleting, setDeleting] = useState(false);
  const [oauthProviders, setOauthProviders] = useState<OAuthProvidersResponse | null>(null);
  const objectUrlRef = useRef<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const month = new Date().toISOString().slice(0, 7);

  const clearObjectPreview = useCallback(() => {
    if (objectUrlRef.current) {
      URL.revokeObjectURL(objectUrlRef.current);
      objectUrlRef.current = null;
    }
  }, []);

  const load = useCallback(async () => {
    if (!token) return;
    const [nextUser, bootstrap] = await Promise.all([api.me(token), api.bootstrap(token, month)]);
    clearObjectPreview();
    setUser(nextUser);
    setBoot(bootstrap);
    setPhotoFile(null);
    setPhotoPreview(apiAssetUrl(nextUser.avatar_url));
    setProfile({ name: nextUser.name || "" });
  }, [clearObjectPreview, token, month]);

  useEffect(() => {
    load().catch((err) => setMessage(err instanceof Error ? err.message : "Falha ao carregar."));
  }, [load]);

  useEffect(() => () => clearObjectPreview(), [clearObjectPreview]);

  useEffect(() => {
    api.oauthProviders().then(setOauthProviders).catch(() => undefined);
  }, []);

  useEffect(() => {
    // A volta do vínculo OAuth (SEC-04) chega em ?linked=1, sem passar por
    // useSearchParams para não exigir Suspense nesta página.
    if (new URLSearchParams(window.location.search).get("linked") === "1") {
      setMessage("Conta social vinculada.");
      window.history.replaceState(null, "", "/perfil");
      load().catch(() => undefined);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function updateSidebarUser() {
    window.dispatchEvent(new Event("trevo:user-updated"));
  }

  function selectPhoto(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file) return;

    if (!PHOTO_TYPES.includes(file.type)) {
      setMessage("Envie uma foto JPG, PNG ou WebP.");
      event.target.value = "";
      return;
    }

    if (file.size > PHOTO_MAX_BYTES) {
      setMessage("A foto deve ter no maximo 512 KB.");
      event.target.value = "";
      return;
    }

    clearObjectPreview();
    const nextPreview = URL.createObjectURL(file);
    objectUrlRef.current = nextPreview;
    setPhotoFile(file);
    setPhotoPreview(nextPreview);
    setMessage("Foto selecionada. Salve o perfil para aplicar.");
  }

  async function saveProfile(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!token) return;
    setSavingProfile(true);
    try {
      let nextUser = await api.updateProfile(token, { name: profile.name });
      if (photoFile) {
        nextUser = await api.uploadProfilePhoto(token, photoFile);
      }

      clearObjectPreview();
      setPhotoFile(null);
      setPhotoPreview(apiAssetUrl(nextUser.avatar_url));
      setUser(nextUser);
      updateSidebarUser();
      setMessage("Perfil atualizado.");
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "Falha ao salvar perfil.");
    } finally {
      setSavingProfile(false);
    }
  }

  async function removePhoto() {
    if (!token) return;
    setSavingProfile(true);
    try {
      const nextUser = await api.updateProfile(token, { avatar_url: null });
      clearObjectPreview();
      if (fileInputRef.current) fileInputRef.current.value = "";
      setPhotoFile(null);
      setPhotoPreview("");
      setUser(nextUser);
      updateSidebarUser();
      setMessage("Foto removida.");
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "Falha ao remover foto.");
    } finally {
      setSavingProfile(false);
    }
  }

  async function changePassword(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!token) return;
    const form = new FormData(event.currentTarget);
    await api.changePassword(token, {
      current_password: String(form.get("current_password")),
      new_password: String(form.get("new_password"))
    });
    event.currentTarget.reset();
    setMessage("Senha atualizada.");
  }

  async function exportData() {
    if (!token) return;
    try {
      const res = await fetch(api.exportDataUrl(), {
        credentials: "include",
        headers: token && token !== COOKIE_AUTH_TOKEN ? { Authorization: `Bearer ${token}` } : {}
      });
      if (!res.ok) throw new Error("Falha ao exportar dados.");
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = "trevo-meus-dados.json";
      anchor.click();
      URL.revokeObjectURL(url);
      setMessage("Exportação gerada. Verifique seus downloads.");
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "Falha ao exportar dados.");
    }
  }

  async function deleteAccount(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!token) return;
    if (!window.confirm("Isto apaga permanentemente sua conta e TODOS os seus dados. Esta ação não pode ser desfeita. Continuar?")) {
      return;
    }
    setDeleting(true);
    try {
      await api.deleteAccount(token, deletePassword || undefined);
      clearSession();
      router.replace("/login");
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "Falha ao excluir a conta.");
      setDeleting(false);
    }
  }

  const initials = initialsFromUser(user);
  const displayName = user?.name || profile.name || "Perfil";

  return (
    <Shell>
      <div className="mx-auto max-w-5xl px-4 py-5 sm:py-6">
        <PageHeader
          description={user?.email || ""}
          helpText="Gerencie seus dados pessoais, foto, senha e informacoes da conta. Preferencias gerais ficam em Configuracoes."
          icon={UserRound}
          title="Perfil"
        />

        <FeedbackMessage message={message} />

        <section className="app-card p-4">
          <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
            <div className="flex items-center gap-4">
              <ProfilePhoto initials={initials} name={displayName} preview={photoPreview} />
              <div className="min-w-0">
                <p className="text-xs font-bold uppercase tracking-normal text-muted">Conta Trevo</p>
                <h2 className="truncate text-xl font-black text-ink">{displayName}</h2>
                <p className="mt-1 flex items-center gap-2 text-sm text-muted">
                  <Mail size={15} aria-hidden />
                  <span className="truncate">{user?.email}</span>
                </p>
              </div>
            </div>
            <div className="flex flex-wrap gap-2">
              <button className="btn-secondary" type="button" onClick={() => fileInputRef.current?.click()}>
                <Camera size={16} aria-hidden />
                Escolher foto
              </button>
              {photoPreview ? (
                <button className="btn-secondary" type="button" onClick={removePhoto} disabled={savingProfile}>
                  <Trash2 size={16} aria-hidden />
                  Remover
                </button>
              ) : null}
            </div>
          </div>
        </section>

        <form onSubmit={saveProfile} className="app-card mt-4 p-4">
          <SectionIntro
            title="Dados pessoais"
            description="Nome e foto usados para personalizar sua experiencia."
            action={<Upload size={18} className="text-leaf" />}
          />
          <input
            ref={fileInputRef}
            className="sr-only"
            type="file"
            accept={PHOTO_TYPES.join(",")}
            onChange={selectPhoto}
          />
          <label className="block text-sm font-semibold">
            Nome
            <input className="field mt-1" value={profile.name} onChange={(event) => setProfile({ ...profile, name: event.target.value })} required />
          </label>
          <div className="mt-3 rounded-app border border-line bg-surface-muted/40 p-3">
            <p className="text-sm font-semibold text-ink">Foto de perfil</p>
            <p className="mt-1 text-xs text-muted">JPG, PNG ou WebP ate 512 KB. A foto aparece no Perfil e na sidebar.</p>
          </div>
          <button className="btn-primary mt-4" type="submit" disabled={savingProfile}>
            {savingProfile ? "Salvando..." : "Salvar perfil"}
          </button>
        </form>

        <section className="app-card mt-4 p-4">
          <SectionIntro
            title="Dados financeiros"
            description="Confira os numeros base usados no Resumo e ajuste o planejamento em uma tela dedicada."
            action={<WalletCards size={18} className="text-leaf" />}
          />
          <div className="grid gap-3 md:grid-cols-3">
            <KpiCard label="Salario" value={formatBRL(boot?.settings.monthly_income || 0)} />
            <KpiCard label="Meta diaria" value={formatBRL(boot?.settings.daily_goal || 0)} note={(boot?.settings.daily_goal || 0) > 0 ? "Manual" : "Automatica"} />
            <KpiCard label="Reserva total" value={formatBRL(boot?.settings.reserve_current_amount || 0)} note={`Meta ${formatBRL(boot?.settings.reserve_goal_amount || 0)}`} />
          </div>
          <Link className="btn-secondary mt-4" href="/onboarding">
            <WalletCards size={16} aria-hidden />
            Editar planejamento financeiro
          </Link>
        </section>

        <form onSubmit={changePassword} className="app-card mt-4 p-4">
          <SectionIntro
            title="Senha"
            description="Atualize sua senha quando precisar reforcar a seguranca."
          />
          <div className="grid gap-3 sm:grid-cols-[1fr_1fr_auto]">
            <input className="field" name="current_password" type="password" placeholder="Senha atual" required />
            <input className="field" name="new_password" type="password" placeholder="Nova senha" required />
            <button className="btn-secondary" type="submit">Trocar senha</button>
          </div>
        </form>

        <section className="app-card mt-4 p-4">
          <SectionIntro
            title="Contas sociais"
            description="Vincule uma conta para entrar sem senha da próxima vez. Só é possível vincular uma conta social por vez, com a senha atual confirmada por esta sessão."
          />
          <div className="mt-3 grid gap-2 sm:grid-cols-3">
            {(Object.keys(OAUTH_PROVIDER_LABELS) as OAuthProviderKey[]).map((provider) => {
              const configured = Boolean(oauthProviders?.providers[provider]?.enabled);
              const linked = user?.auth_provider === provider;
              return (
                <button
                  key={provider}
                  type="button"
                  className="btn-secondary w-full text-xs disabled:cursor-not-allowed disabled:opacity-50"
                  disabled={!configured || linked}
                  title={
                    linked
                      ? "Já vinculado a esta conta"
                      : !configured
                        ? `${OAUTH_PROVIDER_LABELS[provider]} não configurado neste ambiente.`
                        : undefined
                  }
                  onClick={() => {
                    window.location.href = api.oauthLinkUrl(provider);
                  }}
                >
                  {linked ? `${OAUTH_PROVIDER_LABELS[provider]} vinculado` : `Vincular ${OAUTH_PROVIDER_LABELS[provider]}`}
                </button>
              );
            })}
          </div>
        </section>

        <section className="app-card mt-4 p-4">
          <SectionIntro
            title="Privacidade e dados (LGPD)"
            description="Você pode baixar todos os seus dados ou excluir permanentemente sua conta a qualquer momento."
            action={<ShieldAlert size={18} className="text-leaf" />}
          />
          <button className="btn-secondary" type="button" onClick={exportData}>
            <Download size={16} aria-hidden />
            Exportar meus dados (JSON)
          </button>

          <form onSubmit={deleteAccount} className="mt-4 rounded-app border border-danger/40 bg-danger/5 p-3">
            <p className="text-sm font-bold text-danger">Excluir conta</p>
            <p className="mt-1 text-xs text-muted">
              Apaga permanentemente sua conta, transações, cartões, orçamentos e foto. Não é reversível.
            </p>
            <div className="mt-3 grid gap-3 sm:grid-cols-[1fr_auto]">
              <input
                className="field"
                type="password"
                placeholder="Confirme sua senha (deixe vazio se usou login social)"
                value={deletePassword}
                onChange={(event) => setDeletePassword(event.target.value)}
                autoComplete="current-password"
              />
              <button className="btn-secondary border-danger text-danger" type="submit" disabled={deleting}>
                <Trash2 size={16} aria-hidden />
                {deleting ? "Excluindo..." : "Excluir minha conta"}
              </button>
            </div>
          </form>
        </section>
      </div>
    </Shell>
  );
}
