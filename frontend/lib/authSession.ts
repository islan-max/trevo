import { clearApiCache } from "@/lib/api";

export const COOKIE_AUTH_TOKEN = "__trevo_cookie_session__";

const SESSION_HINT_KEY = "trevo_session_active";
const LEGACY_SESSION_TOKEN_KEY = "rf_token";
const LEGACY_LOCAL_TOKEN_KEY = "trevo_token";

export function rememberSession() {
  window.localStorage.setItem(SESSION_HINT_KEY, "1");
  window.sessionStorage.removeItem(LEGACY_SESSION_TOKEN_KEY);
  window.localStorage.removeItem(LEGACY_LOCAL_TOKEN_KEY);
  // Uma nova sessão começa com o cache de GET vazio — sem isto, a resposta
  // de um usuário anterior na mesma aba (ainda dentro da janela de 30s do
  // cache) apareceria para quem acabou de logar. Ver SEC-01.
  clearApiCache();
}

export function clearSession() {
  window.localStorage.removeItem(SESSION_HINT_KEY);
  window.sessionStorage.removeItem(LEGACY_SESSION_TOKEN_KEY);
  window.localStorage.removeItem(LEGACY_LOCAL_TOKEN_KEY);
  // Mesmo raciocínio do lado do logout: sem isto, o próximo usuário a logar
  // nesta aba ainda veria dados em cache do usuário que saiu. Ver SEC-01.
  clearApiCache();
}

export function hasSessionHint() {
  return window.localStorage.getItem(SESSION_HINT_KEY) === "1";
}
