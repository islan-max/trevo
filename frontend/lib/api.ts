import type {
  Bootstrap,
  BudgetSummary,
  Card,
  Category,
  CsvImportMode,
  CsvPreview,
  CsvUpload,
  Goal,
  ReportSummary,
  Settings,
  Transaction,
  TransactionType,
  User
} from "@/types/finance";
import { COOKIE_AUTH_TOKEN } from "@/lib/authSession";

const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL || "";
const GET_CACHE_TTL_MS = 30_000;

type RequestOptions = RequestInit & {
  token?: string | null;
};

type CacheEntry = {
  data: unknown;
  timestamp: number;
};

const responseCache = new Map<string, CacheEntry>();
const pendingRequests = new Map<string, Promise<unknown>>();

// Identifica a sessão atual para a chave do cache de GET. A sessão por cookie
// sempre passa a MESMA constante COOKIE_AUTH_TOKEN como "token" — sem isto,
// a chave do cache é idêntica para qualquer usuário autenticado por cookie, e
// a resposta de um fica visível para o próximo que logar na mesma aba. É
// atualizada a cada resposta de /api/auth/me (chamada ao validar a sessão) e
// zerada por clearApiCache(). Ver SEC-01 em docs/auditoria-2026-09.md.
let activeUserId: string | null = null;

/**
 * Esvazia o cache de respostas e o identificador de sessão. Deve ser chamada
 * sempre que a identidade autenticada muda — logout, login e registro bem-
 * sucedidos — para que a resposta de um usuário nunca sobreviva para o
 * próximo. `clearSession()`/`rememberSession()` em lib/authSession.ts já
 * chamam isto; não é preciso chamar manualmente nas páginas.
 */
export function clearApiCache(): void {
  responseCache.clear();
  pendingRequests.clear();
  activeUserId = null;
}

const CSRF_COOKIE = "trevo_csrf";
const CSRF_HEADER = "X-CSRF-Token";
const CSRF_EXEMPT_PATHS = new Set(["/api/auth/login", "/api/auth/register"]);

function readCookie(name: string): string | null {
  if (typeof document === "undefined") return null;
  const match = document.cookie.match(new RegExp(`(?:^|; )${name}=([^;]*)`));
  return match ? decodeURIComponent(match[1]) : null;
}

async function ensureCsrfToken(): Promise<string | null> {
  const existing = readCookie(CSRF_COOKIE);
  if (existing) return existing;
  try {
    const res = await fetch(`${API_BASE_URL}/api/auth/csrf`, { credentials: "include" });
    if (res.ok) {
      const data = await res.json().catch(() => null);
      if (data?.csrf_token) return data.csrf_token as string;
    }
  } catch {
    // ignore — request will fail with a clear CSRF error if truly missing
  }
  return readCookie(CSRF_COOKIE);
}

function cacheKey(path: string, token?: string | null) {
  return `${token || "public"}::${activeUserId ?? ""}::${path}`;
}

function invalidateTokenCache(token?: string | null) {
  const prefix = `${token || "public"}::`;
  for (const key of responseCache.keys()) {
    if (key.startsWith(prefix)) responseCache.delete(key);
  }
}

export function apiAssetUrl(value?: string | null) {
  if (!value) return "";
  if (value.startsWith("data:") || value.startsWith("http://") || value.startsWith("https://")) return value;
  return `${API_BASE_URL}${value}`;
}

function withQuery(path: string, params: Record<string, string | number | undefined | null>) {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") search.set(key, String(value));
  });
  const qs = search.toString();
  return qs ? `${path}?${qs}` : path;
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const method = (options.method || "GET").toUpperCase();
  const isGet = method === "GET";
  const key = cacheKey(path, options.token);

  if (isGet) {
    const cached = responseCache.get(key);
    if (cached && Date.now() - cached.timestamp < GET_CACHE_TTL_MS) return cached.data as T;

    const pending = pendingRequests.get(key);
    if (pending) return pending as Promise<T>;
  }

  const headers = new Headers(options.headers);
  const usingBearer = Boolean(options.token && options.token !== COOKIE_AUTH_TOKEN);
  if (usingBearer) headers.set("Authorization", `Bearer ${options.token}`);
  if (options.body && !(options.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  // Cookie-authenticated state changes need the CSRF double-submit header.
  if (!isGet && !usingBearer && !CSRF_EXEMPT_PATHS.has(path)) {
    const csrf = await ensureCsrfToken();
    if (csrf) headers.set(CSRF_HEADER, csrf);
  }

  const promise = fetch(`${API_BASE_URL}${path}`, {
    ...options,
    headers,
    cache: "no-store",
    credentials: "include"
  }).then(async (response) => {
    if (!response.ok) {
      const error = await response.json().catch(() => ({ detail: "Falha na requisição." }));
      throw new Error(error.detail || error.error || "Falha na requisição.");
    }

    const data = await response.json();
    if (isGet) {
      responseCache.set(key, { data, timestamp: Date.now() });
    } else {
      invalidateTokenCache(options.token);
    }
    return data as T;
  });

  if (isGet) {
    pendingRequests.set(key, promise);
    promise.finally(() => pendingRequests.delete(key)).catch(() => undefined);
  }

  return promise;
}

export type TransactionFilters = {
  month?: string;
  type?: TransactionType | "";
  categoryId?: number | "";
  paymentMethod?: string;
  source?: string;
  cardId?: number | "";
  search?: string;
};

export type OAuthProviderKey = "google" | "github" | "facebook";

export type OAuthProviderStatus = {
  enabled: boolean;
  configured: boolean;
  redirect_ready: boolean;
};

export type OAuthProvidersResponse = {
  providers: Record<OAuthProviderKey, OAuthProviderStatus>;
};

export const api = {
  oauthProviders() {
    return request<OAuthProvidersResponse>("/api/auth/oauth/providers");
  },
  oauthAuthorizeUrl(provider: OAuthProviderKey) {
    return `${API_BASE_URL}/api/auth/oauth/${provider}/authorize`;
  },
  register(payload: { name: string; email: string; password: string; acceptTerms: boolean }) {
    const { acceptTerms, ...rest } = payload;
    return request<{ access_token: string; token_type: string }>("/api/auth/register", {
      method: "POST",
      body: JSON.stringify({ ...rest, accept_terms: acceptTerms })
    });
  },
  deleteAccount(token: string, password?: string) {
    return request<{ deleted: boolean }>("/api/auth/me", {
      method: "DELETE",
      token,
      body: JSON.stringify({ password: password ?? null })
    });
  },
  updateConsent(token: string, payload: { scope: "monthly_summary" | "terms_privacy"; granted: boolean }) {
    return request<{ scope: string; granted: boolean; policy_version: string }>("/api/privacy/consent", {
      method: "POST",
      token,
      body: JSON.stringify(payload)
    });
  },
  exportDataUrl() {
    return `${API_BASE_URL}/api/privacy/export`;
  },
  login(email: string, password: string) {
    const body = new URLSearchParams({ email, password });
    return request<{ access_token: string; token_type: string }>("/api/auth/login", {
      method: "POST",
      body,
      headers: { "Content-Type": "application/x-www-form-urlencoded" }
    });
  },
  me(token: string) {
    // Captura o id do usuário para diferenciar a chave do cache por sessão
    // (ver activeUserId acima) — necessário porque toda sessão por cookie
    // usa o mesmo "token" (COOKIE_AUTH_TOKEN).
    return request<User>("/api/auth/me", { token }).then((user) => {
      activeUserId = user.id;
      return user;
    });
  },
  updateProfile(token: string, payload: Partial<Pick<User, "name" | "avatar_url" | "send_monthly_summary">>) {
    return request<User>("/api/auth/me", { method: "PUT", token, body: JSON.stringify(payload) });
  },
  uploadProfilePhoto(token: string, file: File) {
    const form = new FormData();
    form.set("file", file);
    return request<User>("/api/auth/me/avatar", { method: "POST", token, body: form });
  },
  changePassword(token: string, payload: { current_password: string; new_password: string }) {
    return request<{ ok: boolean }>("/api/auth/change-password", { method: "POST", token, body: JSON.stringify(payload) });
  },
  logout(token: string) {
    return request<{ message: string }>("/api/auth/logout", { method: "POST", token });
  },
  bootstrap(token: string, month: string) {
    return request<Bootstrap>(withQuery("/api/bootstrap", { month }), { token });
  },
  goals(token: string, month: string) {
    return request<Goal>(withQuery("/api/goals", { month }), { token });
  },
  settings(token: string, payload: Partial<{
    monthlyIncome: number;
    dailyGoal: number;
    reserveAmount: number;
    reserveGoalAmount: number;
    reserveCurrentAmount: number;
  }>) {
    return request<Settings>("/api/settings", { method: "POST", token, body: JSON.stringify(payload) });
  },
  cards(token: string, month: string) {
    return request<Card[]>(withQuery("/api/cards", { month }), { token });
  },
  createCard(token: string, payload: {
    name: string;
    brand: string;
    lastFour: string;
    creditLimit: number;
    closingDay: number;
    dueDay: number;
    color: string;
  }) {
    return request<Card>("/api/cards", { method: "POST", token, body: JSON.stringify(payload) });
  },
  updateCard(token: string, cardId: number, payload: Partial<{
    name: string;
    brand: string;
    lastFour: string;
    creditLimit: number;
    closingDay: number;
    dueDay: number;
    color: string;
  }>) {
    return request<Card>(`/api/cards/${cardId}`, { method: "PUT", token, body: JSON.stringify(payload) });
  },
  deleteCard(token: string, cardId: number, force = false) {
    return request<{ deleted: boolean; unlinkedTransactions: number }>(withQuery(`/api/cards/${cardId}`, { force: String(force) }), { method: "DELETE", token });
  },
  createInstallments(token: string, cardId: number, payload: {
    title: string;
    categoryId?: number | null;
    totalAmount: number;
    totalInstallments: number;
    purchaseDate: string;
    notes?: string;
  }) {
    return request<{ createdInstallments: number; group: string; rows: Transaction[] }>(`/api/cards/${cardId}/installments`, {
      method: "POST",
      token,
      body: JSON.stringify(payload)
    });
  },
  purchaseSimulation(token: string, cardId: number, payload: { totalAmount: number; totalInstallments: number; purchaseDate: string; months?: number }) {
    return request<{ projection: Array<{ month: string; currentInvoice: number; simulatedInstallment: number; projectedTotal: number }> }>(
      `/api/cards/${cardId}/purchase-simulation`,
      { method: "POST", token, body: JSON.stringify(payload) }
    );
  },
  simulateInstallments(token: string, payload: { totalAmount: number; totalInstallments: number; interestRate?: number; purchaseDate: string; months?: number }) {
    return request<{ projection: Array<{ month: string; simulatedInstallment: number; projectedTotal: number }> }>(
      "/api/installments/simulate",
      { method: "POST", token, body: JSON.stringify(payload) }
    );
  },
  createInstallmentsWithoutCard(token: string, payload: {
    title: string;
    categoryId?: number | null;
    totalAmount: number;
    totalInstallments: number;
    interestRate?: number;
    purchaseDate: string;
    notes?: string;
  }) {
    return request<{ createdInstallments: number; group: string }>("/api/installments", {
      method: "POST",
      token,
      body: JSON.stringify(payload)
    });
  },
  getFutureInstallments(token: string, filters: { month?: string; limit?: number }) {
    return request<{ installments: Array<{ id: number; group: string; month: string; title: string; categoryName: string; amount: number; installmentNumber: number; totalInstallments: number }>; totalMonthlyCommitment: number }>(
      withQuery("/api/installments/future", filters),
      { token }
    );
  },
  categories(token: string, payload: Pick<Category, "name" | "type" | "color" | "icon">) {
    return request<Category>("/api/categories", { method: "POST", token, body: JSON.stringify(payload) });
  },
  deleteCategory(token: string, id: number) {
    return request<{ deleted: boolean; archived: boolean; linkedTransactions: number }>(`/api/categories/${id}`, { method: "DELETE", token });
  },
  transactions(token: string, filters: TransactionFilters) {
    return request<Transaction[]>(withQuery("/api/transactions", filters), { token });
  },
  transaction(token: string, payload: {
    title: string;
    amount: number;
    type: TransactionType;
    categoryId?: number | null;
    paymentMethod: string;
    transactionDate: string;
    notes?: string;
    cardId?: number | null;
    billingMonth?: string | null;
    isRecurring?: boolean;
    recurrenceType?: "monthly" | "weekly" | null;
    recurrenceDay?: number | null;
  }) {
    return request<Transaction>("/api/transactions", { method: "POST", token, body: JSON.stringify(payload) });
  },
  updateTransaction(token: string, id: number, payload: Partial<{
    title: string;
    amount: number;
    type: TransactionType;
    categoryId: number | null;
    paymentMethod: string;
    transactionDate: string;
    notes: string;
    cardId: number | null;
    billingMonth: string | null;
  }>) {
    return request<Transaction>(`/api/transactions/${id}`, { method: "PUT", token, body: JSON.stringify(payload) });
  },
  deleteTransaction(token: string, id: number) {
    return request<{ deleted?: boolean; deletedGroup?: boolean }>(`/api/transactions/${id}`, { method: "DELETE", token });
  },
  budgets(token: string, month: string) {
    return request<BudgetSummary>(withQuery("/api/budgets", { month }), { token });
  },
  saveBudget(token: string, payload: { categoryId: number; month: string; plannedAmount: number }) {
    return request<{ id: number }>("/api/budgets", { method: "POST", token, body: JSON.stringify(payload) });
  },
  deleteBudget(token: string, id: number) {
    return request<{ deleted: boolean }>(`/api/budgets/${id}`, { method: "DELETE", token });
  },
  copyBudget(token: string, payload: { fromMonth: string; toMonth: string }) {
    return request<BudgetSummary>("/api/budgets/copy", { method: "POST", token, body: JSON.stringify(payload) });
  },
  reports(token: string, month: string) {
    return request<ReportSummary>(withQuery("/api/reports", { month }), { token });
  },
  uploadCsv(token: string, file: File) {
    const form = new FormData();
    form.set("file", file);
    return request<CsvUpload>("/api/imports/csv/upload", { method: "POST", token, body: form });
  },
  previewCsv(token: string, payload: { importToken: string; mapping: { date: string; description: string; value: string; type?: string | null; category?: string | null; account?: string | null; time?: string | null } }) {
    return request<CsvPreview>("/api/imports/csv/preview", { method: "POST", token, body: JSON.stringify(payload) });
  },
  confirmCsv(token: string, payload: { importToken: string; mapping: { date: string; description: string; value: string; type?: string | null; category?: string | null; account?: string | null; time?: string | null }; mode: CsvImportMode }) {
    return request<{ imported: number; duplicates: number; invalidRows: number; replaced: number; mode: CsvImportMode; months: Array<{ month: string; label: string }>; transactions: Transaction[] }>("/api/imports/csv/confirm", {
      method: "POST",
      token,
      body: JSON.stringify(payload)
    });
  },
  rules(token: string) {
    return request<Array<{ id: number; pattern: string; category_id: number; category_name: string }>>("/api/categorization-rules", { token });
  },
  createRule(token: string, payload: { pattern: string; categoryId: number; paymentMethod?: string | null }) {
    return request<{ id: number }>("/api/categorization-rules", { method: "POST", token, body: JSON.stringify(payload) });
  },
  exportCsvUrl(month: string) {
    return `${API_BASE_URL}${withQuery("/api/export/csv", { month })}`;
  },
  exportPdfUrl(month: string) {
    return `${API_BASE_URL}${withQuery("/api/export/pdf", { month })}`;
  }
};
