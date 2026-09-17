"use client";

import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { ArrowDownCircle, ArrowUpCircle, LoaderCircle, Pencil, Plus, ReceiptText, Trash2 } from "@/components/icons";
import { EmptyState } from "@/components/EmptyState";
import { ExpenseForm, type MovementFormState } from "@/components/ExpenseForm";
import { FeedbackMessage } from "@/components/FeedbackMessage";
import { FirstTimeExplainer } from "@/components/FirstTimeExplainer";
import { IconButton } from "@/components/IconButton";
import { IncomeForm } from "@/components/IncomeForm";
import { MovementTabs } from "@/components/MovementTabs";
import { PageHeader } from "@/components/PageHeader";
import { SearchInput } from "@/components/SearchInput";
import { Select } from "@/components/Select";
import { SectionIntro } from "@/components/SectionIntro";
import { Shell } from "@/components/Shell";
import { api } from "@/lib/api";
import { formatBRL } from "@/lib/format";
import { useAuthToken } from "@/lib/useAuthToken";
import type { Bootstrap, Category, Transaction, TransactionType } from "@/types/finance";

function defaultDate(month: string) {
  const today = new Date().toISOString().slice(0, 10);
  return today.startsWith(month) ? today : `${month}-01`;
}

const emptyForm = (month: string, type: TransactionType = "expense"): MovementFormState => ({
  title: "",
  amount: 0,
  type,
  categoryId: "",
  paymentMethod: "pix",
  transactionDate: defaultDate(month),
  cardId: "",
  notes: "",
  isRecurring: false
});

const TRANSACTIONS_PAGE_SIZE = 50;

export default function TransacoesPage() {
  const token = useAuthToken();
  const [month] = useState(new Date().toISOString().slice(0, 7));
  const [categories, setCategories] = useState<Category[]>([]);
  const [items, setItems] = useState<Transaction[]>([]);
  const [monthItems, setMonthItems] = useState<Transaction[]>([]);
  const [form, setForm] = useState<MovementFormState>(() => emptyForm(month));
  const [search, setSearch] = useState("");
  const [typeFilter, setTypeFilter] = useState<TransactionType | "">("expense");
  const [sourceFilter, setSourceFilter] = useState("");
  const [message, setMessage] = useState("");
  const [saving, setSaving] = useState(false);
  const [hasMore, setHasMore] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);

  const load = useCallback(async () => {
    if (!token) return;
    const [boot, page] = await Promise.all([
      api.bootstrap(token, month) as Promise<Bootstrap>,
      api.transactions(token, { month, search, type: typeFilter, source: sourceFilter, limit: TRANSACTIONS_PAGE_SIZE, offset: 0 })
    ]);
    setCategories(boot.categories);
    setMonthItems(boot.transactions);
    setItems(page.items);
    setHasMore(page.hasMore);
  }, [token, month, search, typeFilter, sourceFilter]);

  useEffect(() => {
    load().catch((err) => setMessage(err instanceof Error ? err.message : "Falha ao carregar."));
  }, [load]);

  // DB-08: a lista vinha truncada num LIMIT 250 fixo, sem indicar que havia
  // mais. Agora o backend pagina de verdade (limit/offset + hasMore).
  async function loadMore() {
    if (!token || loadingMore) return;
    setLoadingMore(true);
    try {
      const page = await api.transactions(token, {
        month,
        search,
        type: typeFilter,
        source: sourceFilter,
        limit: TRANSACTIONS_PAGE_SIZE,
        offset: items.length
      });
      setItems((current) => [...current, ...page.items]);
      setHasMore(page.hasMore);
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "Falha ao carregar mais movimentações.");
    } finally {
      setLoadingMore(false);
    }
  }

  const totals = useMemo(() => ({
    expense: monthItems.filter((item) => item.type === "expense").length,
    income: monthItems.filter((item) => item.type === "income").length,
    all: monthItems.length
  }), [monthItems]);

  async function handleCreateCategory(category: { name: string; type: TransactionType; color: string; icon: string }) {
    if (!token) throw new Error("Sessao expirada.");
    setSaving(true);
    try {
      const newCategory = await api.categories(token, category);
      setCategories((current) => [...current.filter((item) => item.id !== newCategory.id), newCategory]);
      setMessage("Categoria criada com sucesso.");
      return newCategory;
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "Falha ao criar categoria.");
      throw err;
    }
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!token) return;
    if (form.amount <= 0) {
      setMessage("Informe um valor maior que zero.");
      return;
    }
    const recurringDay = Number(form.transactionDate.slice(8, 10));
    const payload = {
      title: form.title,
      amount: form.amount,
      type: form.type,
      categoryId: form.categoryId ? Number(form.categoryId) : null,
      paymentMethod: form.type === "income" ? "pix" : form.paymentMethod,
      transactionDate: form.transactionDate,
      cardId: form.type === "expense" && form.cardId ? Number(form.cardId) : null,
      billingMonth: form.type === "expense" && form.cardId ? month : null,
      notes: form.notes,
      isRecurring: form.type === "income" ? form.isRecurring : false,
      recurrenceType: form.type === "income" && form.isRecurring ? "monthly" as const : null,
      recurrenceDay: form.type === "income" && form.isRecurring ? recurringDay : null
    };
    try {
      if (form.id) {
        await api.updateTransaction(token, form.id, {
          title: payload.title,
          amount: payload.amount,
          type: payload.type,
          categoryId: payload.categoryId,
          paymentMethod: payload.paymentMethod,
          transactionDate: payload.transactionDate,
          cardId: payload.cardId,
          billingMonth: payload.billingMonth,
          notes: payload.notes
        });
      } else {
        await api.transaction(token, payload);
      }
      setForm(emptyForm(month, form.type));
      setMessage(form.id ? "Movimentação atualizada." : form.type === "income" ? "Entrada cadastrada." : "Despesa cadastrada.");
      await load();
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "Falha ao salvar.");
    } finally {
      setSaving(false);
    }
  }

  async function handleDelete(item: Transaction) {
    if (!token) return;
    const isInstallment = item.type === "expense" && (item.total_installments ?? 1) > 1;
    const confirmDelete = window.confirm(
      isInstallment
        ? `Excluir "${item.title}"? Como esta compra é parcelada, todas as parcelas também serão removidas.`
        : `Excluir "${item.title}"?`
    );
    if (!confirmDelete) return;
    try {
      // scope=group precisa acompanhar o aviso acima — a API agora recusa
      // (409) apagar uma parcela sem confirmação explícita de que o grupo
      // inteiro sai junto.
      await api.deleteTransaction(token, item.id, isInstallment ? "group" : "single");
      setMessage("Movimentação excluída.");
      await load();
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "Falha ao excluir.");
    }
  }

  function startNew(type: TransactionType) {
    setForm(emptyForm(month, type));
    setTypeFilter(type);
  }

  function edit(item: Transaction) {
    setForm({
      id: item.id,
      title: item.title,
      amount: item.amount,
      type: item.type,
      categoryId: item.category_id ? String(item.category_id) : "",
      paymentMethod: item.payment_method,
      transactionDate: item.transaction_date,
      cardId: item.card_id ? String(item.card_id) : "",
      notes: item.notes || "",
      isRecurring: false
    });
    setTypeFilter(item.type);
  }

  return (
    <Shell>
      <div className="mx-auto max-w-6xl px-4 py-5 sm:py-6">
        <PageHeader
          description="Separe entradas e despesas sem precisar entender termos técnicos."
          helpText="Cadastre entradas e despesas para acompanhar seu fluxo financeiro. Use categorias e formas de pagamento para entender seus hábitos."
          icon={ReceiptText}
          title="Movimentações"
        />

        <FirstTimeExplainer
          storageKey="rf_seen_movements_intro"
          title="Entradas e despesas agora ficam mais claras"
          description="Use Nova despesa para gastos e Nova entrada para salário, freelas ou outras receitas. A lista abaixo pode mostrar tudo junto ou separado."
        />

        <FeedbackMessage message={message} />

        <section className="mb-4 grid gap-3 sm:grid-cols-2">
          <button
            className="focus-ring rounded-app border border-danger/25 bg-danger/10 p-4 text-left shadow-soft hover:bg-danger/15 transition"
            type="button"
            onClick={() => startNew("expense")}
            aria-label="Cadastrar nova despesa"
          >
            <ArrowDownCircle className="text-danger" size={22} />
            <strong className="mt-2 block">Nova despesa</strong>
            <span className="mt-1 block text-sm text-muted">Gasto, compra, conta ou pagamento feito no mês.</span>
          </button>
          <button
            className="focus-ring rounded-app border border-success/25 bg-success/10 p-4 text-left shadow-soft hover:bg-success/15 transition"
            type="button"
            onClick={() => startNew("income")}
            aria-label="Cadastrar nova entrada"
          >
            <ArrowUpCircle className="text-success" size={22} />
            <strong className="mt-2 block">Nova entrada</strong>
            <span className="mt-1 block text-sm text-muted">Salário, receita extra, freelance ou dinheiro recebido.</span>
          </button>
        </section>

        <form onSubmit={handleSubmit} className="app-card p-4">
          <SectionIntro
            title={form.type === "income" ? "Cadastrar entrada" : "Cadastrar despesa"}
            description={form.type === "income" ? "Registre dinheiro que entrou no mês." : "Registre gastos com descrição, valor, categoria e forma de pagamento."}
            helpText="Entradas aumentam seu saldo. Despesas reduzem o saldo e entram nas metas, categorias e orçamento."
          />
          {form.type === "income" ? (
            <IncomeForm
              form={form}
              categories={categories}
              onChange={setForm}
              onCreateCategory={handleCreateCategory}
            />
          ) : (
            <ExpenseForm
              form={form}
              categories={categories}
              onChange={setForm}
              onCreateCategory={handleCreateCategory}
            />
          )}
          <div className="mt-4 flex flex-wrap gap-2">
            <button className="btn-primary" disabled={saving} type="submit">
              {saving ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <Plus size={16} />}
              {form.id ? "Salvar edição" : form.type === "income" ? "Cadastrar entrada" : "Cadastrar despesa"}
            </button>
            {form.id ? (
              <button className="btn-secondary" type="button" onClick={() => setForm(emptyForm(month, form.type))}>
                Cancelar
              </button>
            ) : null}
          </div>
        </form>

        <section className="mt-4">
          <SectionIntro
            title="Lista do mês"
            description="Veja rapidamente o que entrou, o que saiu e de onde veio cada movimentação."
            helpText="Use as abas para separar despesas, entradas ou conferir tudo junto."
          />
          <MovementTabs value={typeFilter} onChange={setTypeFilter} totals={totals} />
          <div className="app-card mt-3 p-4">
            <div className="grid gap-3 md:grid-cols-[1fr_180px_auto]">
              <div className="text-sm">
                <span className="font-semibold text-ink block mb-1">Buscar</span>
                <SearchInput
                  value={search}
                  onChange={setSearch}
                  placeholder="Buscar por nome, categoria ou descricao"
                  showIcon={false}
                />
              </div>
              <div className="text-sm">
                <span className="font-semibold text-ink block mb-1">Origem</span>
                <Select
                  value={sourceFilter}
                  onChange={(value) => setSourceFilter(String(value))}
                  options={[
                    { value: "", label: "Todas" },
                    { value: "manual", label: "Manual" },
                    { value: "csv_import", label: "CSV" },
                    { value: "open_finance_future", label: "Open Finance futuro" }
                  ]}
                  placeholder="Selecione a origem"
                />
              </div>
              <button
                className="btn-secondary self-end"
                type="button"
                onClick={() => load().catch(console.error)}
                aria-label="Aplicar filtros"
              >
                Filtrar
              </button>
            </div>

            <div className="mt-4 divide-y divide-line">
              {items.map((item) => (
                <article
                  key={item.id}
                  className="flex flex-col gap-3 py-3 sm:flex-row sm:items-center sm:justify-between"
                >
                  <div className="min-w-0">
                    <span
                      className={
                        item.type === "income"
                          ? "mb-1 inline-flex rounded-app bg-success/10 px-2 py-1 text-xs font-semibold text-success"
                          : "mb-1 inline-flex rounded-app bg-danger/10 px-2 py-1 text-xs font-semibold text-danger"
                      }
                    >
                      {item.type === "income" ? "Entrada" : "Despesa"}
                    </span>
                    <h2 className="truncate text-sm font-semibold">{item.title}</h2>
                    <p className="text-xs text-muted">
                      {item.transaction_date} / {item.category_name || "Sem categoria"} / {item.payment_method} / {item.source}
                    </p>
                  </div>
                  <div className="flex items-center justify-between gap-2 sm:justify-end">
                    <strong className={item.type === "income" ? "text-success" : "text-danger"}>
                      {item.type === "income" ? "+" : "-"}
                      {formatBRL(item.amount)}
                    </strong>
                    <IconButton icon={Pencil} label={`Editar ${item.type === "income" ? "entrada" : "despesa"} ${item.title}`} onClick={() => edit(item)} />
                    <IconButton icon={Trash2} label={`Excluir ${item.type === "income" ? "entrada" : "despesa"} ${item.title}`} onClick={() => handleDelete(item).catch(console.error)} />
                  </div>
                </article>
              ))}
              {!items.length ? (
                <div className="py-4">
                  <EmptyState
                    title={
                      typeFilter === "income"
                        ? "Nenhuma entrada lançada ainda"
                        : typeFilter === "expense"
                        ? "Nenhuma despesa lançada ainda"
                        : "Nenhuma movimentação encontrada"
                    }
                    description={
                      typeFilter === "income"
                        ? "Cadastre seu salário ou uma receita extra para acompanhar o dinheiro que entrou."
                        : "Cadastre seus gastos do mês para entender para onde seu dinheiro está indo."
                    }
                    actionLabel={typeFilter === "income" ? "Cadastrar entrada" : "Cadastrar despesa"}
                    onAction={() => startNew(typeFilter || "expense")}
                    icon={typeFilter === "income" ? ArrowUpCircle : ArrowDownCircle}
                  />
                </div>
              ) : null}
              {hasMore ? (
                <div className="py-4 text-center">
                  <button className="btn-secondary" disabled={loadingMore} onClick={() => loadMore()} type="button">
                    {loadingMore ? <LoaderCircle className="animate-spin" size={16} aria-hidden /> : null}
                    {loadingMore ? "Carregando..." : "Carregar mais"}
                  </button>
                </div>
              ) : null}
            </div>
          </div>
        </section>
      </div>
    </Shell>
  );
}
