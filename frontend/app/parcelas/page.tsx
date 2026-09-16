"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import { Calculator, Plus, Trash2, TrendingUp } from "@/components/icons";
import { EmptyState } from "@/components/EmptyState";
import { FeedbackMessage } from "@/components/FeedbackMessage";
import { FirstTimeExplainer } from "@/components/FirstTimeExplainer";
import { IconButton } from "@/components/IconButton";
import { MoneyInput } from "@/components/MoneyInput";
import { PageHeader } from "@/components/PageHeader";
import { SectionIntro } from "@/components/SectionIntro";
import { Select } from "@/components/Select";
import { Shell } from "@/components/Shell";
import { api } from "@/lib/api";
import { formatBRL } from "@/lib/format";
import { parseApiMoneyValue } from "@/lib/money";
import { useAuthToken } from "@/lib/useAuthToken";
import type { Category } from "@/types/finance";

type Projection = Array<{ month: string; simulatedInstallment: number; projectedTotal: number }>;
type FutureInstallment = { id: number; group: string; month: string; title: string; categoryName: string; amount: number; installmentNumber: number; totalInstallments: number };

function asNumber(value: FormDataEntryValue | null) {
  return parseApiMoneyValue(String(value || "0"));
}

export default function ParcelasPage() {
  const token = useAuthToken();
  const [month] = useState(new Date().toISOString().slice(0, 7));
  const [categories, setCategories] = useState<Category[]>([]);
  const [message, setMessage] = useState("");
  const [registerCategoryId, setRegisterCategoryId] = useState("");
  const [projection, setProjection] = useState<Projection>([]);
  const [futureInstallments, setFutureInstallments] = useState<FutureInstallment[]>([]);
  const [totalMonthlyCommitment, setTotalMonthlyCommitment] = useState(0);

  const load = useCallback(async () => {
    if (!token) return;
    const boot = await api.bootstrap(token, month);
    setCategories(boot.categories.filter((category) => category.type === "expense"));
  }, [token, month]);

  useEffect(() => {
    load().catch((err) => setMessage(err instanceof Error ? err.message : "Falha ao carregar."));
  }, [load]);

  const loadFutureInstallments = useCallback(async () => {
    if (!token) return;
    try {
      const response = await api.getFutureInstallments(token, { month, limit: 24 });
      setFutureInstallments(response.installments);
      setTotalMonthlyCommitment(response.totalMonthlyCommitment);
    } catch (err) {
      console.error("Erro ao carregar parcelas futuras:", err);
    }
  }, [token, month]);

  useEffect(() => {
    loadFutureInstallments();
  }, [loadFutureInstallments]);

  async function simulate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!token) return;
    const form = new FormData(event.currentTarget);
    try {
      const response = await api.simulateInstallments(token, {
        totalAmount: asNumber(form.get("totalAmount")),
        totalInstallments: Number(form.get("totalInstallments")),
        interestRate: Number(form.get("interestRate") || 0),
        purchaseDate: String(form.get("purchaseDate")),
        months: 12
      });
      setProjection(response.projection);
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "Erro ao simular.");
    }
  }

  async function createInstallmentPurchase(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!token) return;
    const form = new FormData(event.currentTarget);
    try {
      await api.createInstallmentsWithoutCard(token, {
        title: String(form.get("title")),
        categoryId: form.get("categoryId") ? Number(form.get("categoryId")) : null,
        totalAmount: asNumber(form.get("totalAmount")),
        totalInstallments: Number(form.get("totalInstallments")),
        interestRate: Number(form.get("interestRate") || 0),
        purchaseDate: String(form.get("purchaseDate")),
        notes: ""
      });
      setMessage("Compra parcelada criada com sucesso!");
      (event.currentTarget as HTMLFormElement).reset();
      setRegisterCategoryId("");
      setProjection([]);
      // Reload future installments
      await loadFutureInstallments();
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "Erro ao criar parcelas.");
    }
  }

  async function deleteInstallmentPurchase(installment: FutureInstallment) {
    if (!token) return;
    if (!window.confirm(`Excluir "${installment.title}"? Todas as parcelas dessa compra serao removidas.`)) return;
    try {
      // Esta tela já avisa que o grupo inteiro sai junto — scope=group
      // confirma isso para a API, que agora recusa (409) sem essa marca.
      await api.deleteTransaction(token, installment.id, "group");
      setMessage("Compra parcelada excluida.");
      await loadFutureInstallments();
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "Falha ao excluir.");
    }
  }

  return (
    <Shell>
      <div className="mx-auto max-w-6xl px-4 py-5 sm:py-6">
        <PageHeader
          description="Simule e controle compras parceladas para entender o impacto no seu orçamento."
          helpText="Simule e registre compras parceladas sem cadastrar cartão. Veja o impacto nos próximos meses. O Trevo não pede número do cartão nem CVV."
          icon={TrendingUp}
          title="Parcelas"
        />

        <FirstTimeExplainer
          storageKey="trevo_seen_installments_intro"
          title="Simule antes de comprar"
          description="Use o simulador para ver como uma compra parcelada afeta seus próximos meses. Depois salve no seu controle de parcelas."
        />

        <FeedbackMessage message={message} />

        <section className="grid gap-4 xl:grid-cols-2">
          <form onSubmit={simulate} className="app-card p-4">
            <SectionIntro
              title="Simular compra parcelada"
              description="Veja o impacto de uma compra parcelada nos seus próximos meses sem salvar nada ainda."
              helpText="Esta simulação não afeta seu controle. Ela serve apenas para decidir se a compra cabe no seu orçamento."
            />
            <div className="grid gap-3 sm:grid-cols-2">
              <label className="text-sm">
                Nome da compra
                <input className="field mt-1" name="title" placeholder="Ex: Notebook, Geladeira" required />
              </label>
              <label className="text-sm">
                Valor total
                <MoneyInput className="field mt-1" name="totalAmount" required />
              </label>
              <label className="text-sm">
                Número de parcelas
                <input className="field mt-1" name="totalInstallments" inputMode="numeric" placeholder="12" min="1" max="36" required />
              </label>
              <label className="text-sm">
                Juros (% ao mês, opcional)
                <input className="field mt-1" name="interestRate" inputMode="decimal" placeholder="0" min="0" max="100" step="0.5" />
              </label>
              <label className="col-span-2 text-sm">
                Data da compra
                <input className="field mt-1" name="purchaseDate" type="date" defaultValue={`${month}-01`} required />
              </label>
            </div>
            <button className="btn-secondary mt-4 w-full" type="submit">
              <Calculator size={16} />
              Simular impacto
            </button>
          </form>

          <form onSubmit={createInstallmentPurchase} className="app-card p-4">
            <SectionIntro
              title="Registrar compra parcelada"
              description="Depois de simular, salve a compra real para acompanhá-la no seu controle de parcelas."
              helpText="As parcelas vão aparecer como despesas nos meses futuros, ajudando você a planejar melhor."
            />
            <div className="grid gap-3 sm:grid-cols-2">
              <label className="text-sm">
                Nome da compra
                <input className="field mt-1" name="title" placeholder="Ex: Notebook, Geladeira" required />
              </label>
              <label className="text-sm">
                Categoria
                <input type="hidden" name="categoryId" value={registerCategoryId} readOnly />
                <Select
                  className="mt-1"
                  value={registerCategoryId}
                  onChange={(value) => setRegisterCategoryId(String(value))}
                  options={[
                    { value: "", label: "Selecionar (opcional)" },
                    ...categories.map((category) => ({ value: String(category.id), label: category.name, color: category.color }))
                  ]}
                  placeholder="Selecionar (opcional)"
                />
              </label>
              <label className="text-sm">
                Valor total
                <MoneyInput className="field mt-1" name="totalAmount" required />
              </label>
              <label className="text-sm">
                Parcelas
                <input className="field mt-1" name="totalInstallments" inputMode="numeric" placeholder="12" min="1" max="36" required />
              </label>
              <label className="text-sm">
                Juros (% ao mês, opcional)
                <input className="field mt-1" name="interestRate" inputMode="decimal" placeholder="0" min="0" max="100" step="0.5" />
              </label>
              <label className="text-sm">
                Data da compra
                <input className="field mt-1" name="purchaseDate" type="date" defaultValue={`${month}-01`} required />
              </label>
            </div>
            <button className="btn-primary mt-4 w-full" type="submit">
              <Plus size={16} />
              Salvar compra parcelada
            </button>
          </form>
        </section>

        {projection.length ? (
          <section className="app-card mt-4 p-4">
            <SectionIntro
              title="Impacto nos próximos meses"
              description="Veja como a parcela apareceria em cada mês e o compromisso projetado."
              helpText="Use isso para decidir se a compra cabe no seu orçamento. Se ficar vermelho (em atenção/estourado), pense bem antes de comprar."
            />
            <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
              {projection.slice(0, 12).map((item) => (
                <div key={item.month} className="rounded-app border border-line p-3 text-sm">
                  <strong className="block">{item.month}</strong>
                  <span className="mt-1 block text-xs text-muted">Parcela</span>
                  <span className="block font-semibold text-success">{formatBRL(item.simulatedInstallment)}</span>
                  <span className="mt-2 block text-xs text-muted">Compromisso estimado</span>
                  <span className="block font-semibold">{formatBRL(item.projectedTotal)}</span>
                </div>
              ))}
            </div>
          </section>
        ) : null}

        <section className="mt-4">
          <SectionIntro
            title="Suas parcelas futuras"
            description="Acompanhe todas as compras parceladas que você registrou e o compromisso total por mês."
            helpText="Dica: se o total mensal ficar alto, considere parcelar em mais vezes ou esperar para fazer outras compras."
          />
          {futureInstallments.length ? (
            <div className="space-y-4">
              <div className="app-card p-4">
                <div className="flex items-end justify-between gap-4">
                  <div>
                    <p className="text-sm text-muted">Compromisso mensal com parcelas (média)</p>
                    <p className="mt-1 text-2xl font-bold text-ink">{formatBRL(totalMonthlyCommitment)}</p>
                  </div>
                  <div className="text-right">
                    <p className="text-xs text-muted">Entre {month} e {futureInstallments[futureInstallments.length - 1]?.month}</p>
                  </div>
                </div>
              </div>

              <div className="grid gap-3">
                {futureInstallments.map((inst) => (
                  <div key={`${inst.group}-${inst.id}`} className="app-card flex items-start justify-between gap-3 p-3">
                    <div>
                      <p className="font-semibold">{inst.title}</p>
                      <p className="text-xs text-muted">
                        {inst.categoryName} • Parcela {inst.installmentNumber} de {inst.totalInstallments}
                      </p>
                    </div>
                    <div className="text-right">
                      <p className="font-semibold">{formatBRL(inst.amount)}</p>
                      <p className="text-xs text-muted">{inst.month}</p>
                    </div>
                    <IconButton icon={Trash2} label={`Excluir compra parcelada ${inst.title}`} onClick={() => deleteInstallmentPurchase(inst).catch((err) => setMessage(err instanceof Error ? err.message : "Falha ao excluir parcelas."))} />
                  </div>
                ))}
              </div>
            </div>
          ) : (
            <EmptyState
              title="Nenhuma compra parcelada registrada"
              description="Use o simulador acima para decidir se quer fazer uma compra parcelada. Depois de simular, salve aqui para acompanhar."
              actionLabel="Simular compra"
              onAction={() => document.querySelector<HTMLInputElement>("input[name='title']")?.focus()}
              icon={TrendingUp}
            />
          )}
        </section>
      </div>
    </Shell>
  );
}
