"use client";

import { useEffect, useState } from "react";
import { CalendarDays } from "@/components/icons";
import { DailyGoalCalendar } from "@/components/DailyGoalCalendar";
import { FeedbackMessage } from "@/components/FeedbackMessage";
import { FirstTimeExplainer } from "@/components/FirstTimeExplainer";
import { KpiCard } from "@/components/KpiCard";
import { PageHeader } from "@/components/PageHeader";
import { SectionIntro } from "@/components/SectionIntro";
import { Shell } from "@/components/Shell";
import { api } from "@/lib/api";
import { formatBRL } from "@/lib/format";
import { useAuthToken } from "@/lib/useAuthToken";
import type { Goal, Transaction } from "@/types/finance";

export default function MetasPage() {
  const token = useAuthToken();
  const [month] = useState(new Date().toISOString().slice(0, 7));
  const [goal, setGoal] = useState<Goal | null>(null);
  const [transactions, setTransactions] = useState<Transaction[]>([]);
  const [message, setMessage] = useState("");

  useEffect(() => {
    if (!token) return;
    Promise.all([api.goals(token, month), api.transactions(token, { month })])
      .then(([nextGoal, nextTransactions]) => {
        setGoal(nextGoal);
        setTransactions(nextTransactions.items);
      })
      .catch((err) => setMessage(err instanceof Error ? err.message : "Falha ao carregar."));
  }, [token, month]);

  async function acceptRecommendedGoal() {
    if (!token || !goal?.recommendedDailyGoal) return;
    await api.settings(token, { dailyGoal: goal.recommendedDailyGoal });
    const nextGoal = await api.goals(token, month);
    setGoal(nextGoal);
    setMessage("Meta diária definida com a recomendação do Trevo.");
  }

  return (
    <Shell>
      <div className="mx-auto max-w-6xl px-4 py-5 sm:py-6">
        <PageHeader
          description={goal?.riskAlert || "Acompanhe seu ritmo dia a dia."}
          helpText="Ajuda a acompanhar quanto você pode gastar por dia para fechar o mês dentro do planejado."
          icon={CalendarDays}
          title="Metas diárias"
        />

        <FirstTimeExplainer
          storageKey="rf_seen_goals_intro"
          title="Cada círculo representa um dia do mês"
          description="Quanto mais cheio, mais perto você chegou da meta diária. Verde está dentro, amarelo pede atenção, vermelho passou da meta e cinza não teve gasto."
        />

        <FeedbackMessage message={message} />

        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <KpiCard
            label="Sua meta diária"
            value={(goal?.dailyGoal || 0) > 0 ? formatBRL(goal?.dailyGoal || 0) : "Não definida"}
            note={(goal?.dailyGoal || 0) > 0 ? "Definida por você" : "Você ainda não definiu uma meta diária"}
          />
          <KpiCard label="Recomendado pelo Trevo" value={formatBRL(goal?.recommendedDailyGoal || 0)} tone="good" />
          <KpiCard label="Permitido restante" value={formatBRL(goal?.allowedRemaining || 0)} tone={(goal?.allowedRemaining || 0) < 0 ? "danger" : "neutral"} />
          <KpiCard label="Projeção de gasto" value={formatBRL(goal?.projectedClosing || 0)} tone={goal?.goalStatus === "red" ? "danger" : goal?.goalStatus === "yellow" ? "warning" : "good"} />
        </div>

        {goal && goal.dailyGoal <= 0 && goal.recommendedDailyGoal > 0 ? (
          <button className="btn-secondary mt-3" type="button" onClick={() => acceptRecommendedGoal().catch((err) => setMessage(err instanceof Error ? err.message : "Falha ao salvar meta."))}>
            Usar recomendação como meta
          </button>
        ) : null}

        <section className="app-card mt-4 p-4">
          <SectionIntro
            title="Calendário do ritmo"
            description="Cada círculo representa um dia do mês. Quanto mais cheio, mais perto você chegou da sua meta diária."
            helpText="Toque em um dia para ver gasto, meta, diferença e as principais despesas daquele dia."
          />
          <DailyGoalCalendar goal={goal} transactions={transactions} />
        </section>

        <section className="mt-4 grid gap-3 md:grid-cols-3">
          <KpiCard label="Média atual" value={formatBRL(goal?.currentAverageSpend || 0)} />
          <KpiCard label="Reserva planejada" value={formatBRL(goal?.reserveAmount || 0)} />
          <KpiCard label="Orçamento mensal" value={formatBRL(goal?.availableBudget || 0)} />
        </section>
      </div>
    </Shell>
  );
}
