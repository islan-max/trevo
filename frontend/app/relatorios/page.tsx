"use client";

import { useEffect, useMemo, useState } from "react";
import dynamic from "next/dynamic";
import { AlertTriangle, Download, FileText, TrendingUp, Wallet } from "@/components/icons";
import { ChartCard } from "@/components/ChartCard";
import { ChartSkeleton } from "@/components/charts/ChartSkeleton";
import { EmptyState } from "@/components/EmptyState";
import { FeedbackMessage } from "@/components/FeedbackMessage";
import { KpiCard } from "@/components/KpiCard";
import { MonthPicker } from "@/components/MonthPicker";
import { PageHeader } from "@/components/PageHeader";
import { SectionIntro } from "@/components/SectionIntro";
import { Shell } from "@/components/Shell";
import { api } from "@/lib/api";
import { COOKIE_AUTH_TOKEN } from "@/lib/authSession";
import { formatBRL } from "@/lib/format";
import { useTheme } from "@/lib/theme";
import { useAuthToken } from "@/lib/useAuthToken";
import type { ReportSummary } from "@/types/finance";

// O recharts responde por ~110 kB do bundle de /relatorios. Sai do carregamento
// inicial e chega junto com os dados, igual ao padrão de SummaryHome.tsx.
const ReportBarChart = dynamic(() => import("@/components/charts/ReportBarChart"), {
  ssr: false,
  loading: () => <ChartSkeleton height={256} label="Carregando gráfico" />
});

async function downloadFile(url: string, token: string, filename: string) {
  const headers = token === COOKIE_AUTH_TOKEN ? undefined : { Authorization: `Bearer ${token}` };
  const response = await fetch(url, { credentials: "include", headers });
  if (!response.ok) throw new Error("Falha ao baixar arquivo.");
  const blob = await response.blob();
  const objectUrl = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = objectUrl;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(objectUrl);
}

function paymentLabel(value: string) {
  const labels: Record<string, string> = {
    boleto: "Boleto",
    cash: "Dinheiro",
    credito: "Crédito",
    credit: "Crédito",
    csv_import: "CSV",
    debito: "Débito",
    debit: "Débito",
    dinheiro: "Dinheiro",
    pix: "Pix",
    transfer: "Transferência"
  };
  return labels[value] || value || "Outro";
}

function GrowthChart({ report }: { report: ReportSummary | null }) {
  const growth = report?.categoryGrowth;
  const rows = growth?.items.slice(0, 8) || [];
  const maxDelta = Math.max(1, ...rows.map((item) => Math.abs(item.delta || 0)));

  if (!growth?.hasHistory) {
    return (
      <EmptyState
        title="Ainda não há histórico suficiente para comparar."
        description="Assim que houver gastos no mês anterior, o Trevo mostra quais categorias cresceram ou diminuíram."
        icon={TrendingUp}
      />
    );
  }

  return (
    <div className="space-y-3">
      {rows.map((item) => {
        const width = Math.max(4, (Math.abs(item.delta) / maxDelta) * 50);
        const positive = item.delta >= 0;
        const percentLabel = item.percentChange === null ? "Novo histórico" : `${item.percentChange > 0 ? "+" : ""}${item.percentChange.toFixed(1)}%`;
        return (
          <article className="interactive-list-item grid gap-2 rounded-app border border-line bg-surface/85 p-3 text-sm shadow-sm md:grid-cols-[150px_1fr_190px]" key={item.name}>
            <div className="min-w-0">
              <strong className="block truncate">{item.name}</strong>
              <span className="text-xs text-muted">{formatBRL(item.previousTotal)} → {formatBRL(item.currentTotal)}</span>
            </div>
            <div className="relative h-9 rounded-app bg-ink/5">
              <span className="absolute bottom-1 top-1 left-1/2 w-px bg-ink/25" aria-hidden />
              <span
                className={positive ? "progress-fill absolute top-2 h-5 rounded-app bg-danger" : "progress-fill absolute top-2 h-5 rounded-app bg-success"}
                style={positive ? { left: "50%", width: `${width}%` } : { right: "50%", width: `${width}%` }}
                aria-hidden
              />
            </div>
            <div className="flex items-center justify-between gap-3 md:justify-end">
              <span className={positive ? "font-semibold text-danger" : "font-semibold text-success"}>
                {positive ? "+" : ""}{formatBRL(item.delta)}
              </span>
              <span className="rounded-app bg-ink/5 px-2 py-1 text-xs font-semibold text-muted">{percentLabel}</span>
            </div>
          </article>
        );
      })}
      {!rows.length ? (
        <EmptyState title="Sem categorias para comparar" description="O relatório precisa de gastos categorizados em pelo menos dois meses." icon={TrendingUp} />
      ) : null}
    </div>
  );
}

export default function RelatoriosPage() {
  const token = useAuthToken();
  const { effectiveTheme } = useTheme();
  const [month, setMonth] = useState(new Date().toISOString().slice(0, 7));
  const [report, setReport] = useState<ReportSummary | null>(null);
  const [message, setMessage] = useState("");
  const [chartsReady, setChartsReady] = useState(false);

  useEffect(() => {
    if (!token) return;
    api.reports(token, month)
      .then((nextReport) => {
        setReport(nextReport);
        setMessage("");
      })
      .catch((err) => setMessage(err instanceof Error ? err.message : "Falha ao carregar."));
  }, [token, month]);

  useEffect(() => {
    setChartsReady(true);
  }, []);

  const categoryRows = report?.dashboard.categoryBreakdown || [];
  const paymentRows = useMemo(
    () => (report?.dashboard.paymentMethodBreakdown || []).map((item) => ({
      ...item,
      label: paymentLabel(item.payment_method),
      total: Number(item.total || 0)
    })),
    [report]
  );
  const trendRows = report?.dashboard.monthlyTrend || [];
  const csvFilename = `trevo-relatorio-${month}.csv`;
  const pdfFilename = `trevo-relatorio-${month}.pdf`;
  const chartGrid = effectiveTheme === "dark" ? "#2D3E55" : "#E5E7EB";
  const chartText = effectiveTheme === "dark" ? "#96A4B8" : "#6D7B8D";
  const chartStroke = effectiveTheme === "dark" ? "#E8EFF7" : "#102033";
  const tooltipStyle = {
    backgroundColor: effectiveTheme === "dark" ? "#0E1B2D" : "#FFFFFF",
    border: `1px solid ${effectiveTheme === "dark" ? "#2D3E55" : "#DDE7F0"}`,
    borderRadius: 12,
    color: effectiveTheme === "dark" ? "#E8EFF7" : "#102033"
  };
  const chartTheme = {
    grid: chartGrid,
    text: chartText,
    stroke: chartStroke,
    cursorFill: effectiveTheme === "dark" ? "#1B2A40" : "#EEF6F8",
    tooltipStyle
  };

  return (
    <Shell>
      <div className="mx-auto max-w-6xl px-4 py-5 sm:py-6">
        <PageHeader
          actions={<MonthPicker value={month} onChange={setMonth} />}
          description="Analise o mês com calma, compare evolução e exporte uma versão profissional."
          helpText="Gere análises do mês, exporte CSV/PDF e entenda onde seu dinheiro está indo."
          icon={FileText}
          title="Relatórios"
        />

        <FeedbackMessage message={message} />

        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
          <KpiCard label="Salário" value={formatBRL(report?.dashboard.salaryBase || 0)} />
          <KpiCard label="Entradas" value={formatBRL(report?.dashboard.inflow || 0)} tone="good" />
          <KpiCard label="Saídas" value={formatBRL(report?.dashboard.outflow || 0)} />
          <KpiCard
            label="Saldo Restante"
            value={formatBRL(report?.dashboard.projectedBalance || 0)}
            note="Considera salário, entradas e despesas do mês."
            tone={(report?.dashboard.projectedBalance || 0) < 0 ? "danger" : "good"}
          />
          <KpiCard label="Meta diária" value={formatBRL(report?.goals.dailyGoal || report?.goals.recommendedDailyGoal || 0)} note={report?.goals.dailyGoal ? "Definida por você" : "Recomendada"} />
        </div>

        <section className="mt-4 flex flex-wrap gap-2">
          <button className="btn-primary" type="button" onClick={() => token && downloadFile(api.exportCsvUrl(month), token, csvFilename).catch((err) => setMessage(err.message))}>
            <Download size={16} aria-hidden />CSV
          </button>
          <button className="btn-secondary" type="button" onClick={() => token && downloadFile(api.exportPdfUrl(month), token, pdfFilename).catch((err) => setMessage(err.message))}>
            <Download size={16} aria-hidden />PDF
          </button>
        </section>

        <section className="app-card mt-4 p-4">
          <SectionIntro
            title="Resumo do relatório"
            description="Visão executiva do mês com score, comprometimento e alertas principais."
            helpText="Use esta área para entender rapidamente se o mês está saudável antes de abrir as seções detalhadas."
          />
          <div className="grid gap-3 lg:grid-cols-[1fr_1fr]">
            <div className="rounded-app border border-line bg-surface/85 p-4">
              <span className="text-xs font-bold uppercase tracking-normal text-muted">Score Trevo</span>
              <strong className="metric-number mt-1 block text-3xl">{report?.score.score || "--"}</strong>
              <p className="text-sm text-muted">{report?.score.label || "Aguardando dados do mês."}</p>
            </div>
            <div className="rounded-app border border-line bg-surface/85 p-4">
              <span className="text-xs font-bold uppercase tracking-normal text-muted">Comprometimento do salário</span>
              <strong className="metric-number mt-1 block text-3xl">{report?.dashboard.salaryCommittedPercent || 0}%</strong>
              <p className="text-sm text-muted">Saídas e reserva planejada sobre o salário base.</p>
            </div>
          </div>
        </section>

        <div className="mt-4 grid gap-4 xl:grid-cols-2">
          <ChartCard title="Categorias">
            <SectionIntro
              title="Gastos por categoria"
              description="Compare onde o dinheiro saiu no mês."
              helpText="As barras usam escala proporcional aos gastos do mês selecionado."
            />
            <div className="h-64">
              {categoryRows.length && chartsReady ? (
                <ReportBarChart
                  data={categoryRows}
                  bars={[{ dataKey: "total", name: "Gasto", fill: "#14B8A6" }]}
                  xAxisDataKey="name"
                  theme={chartTheme}
                  angledLabels
                  margin={{ left: 4, right: 12, bottom: 24 }}
                />
              ) : (
                <EmptyState title="Sem categorias para analisar" description="Cadastre despesas para gerar o relatório por categoria." actionLabel="Cadastrar despesa" href="/transacoes" icon={FileText} />
              )}
            </div>
          </ChartCard>

          <ChartCard title="Crescimento por categoria">
            <SectionIntro
              title="Variação contra o mês anterior"
              description="Barras para a direita indicam aumento de gasto; barras para a esquerda indicam redução."
              helpText="A escala usa o maior delta absoluto do mês, mantendo valores positivos e negativos comparáveis."
            />
            <GrowthChart report={report} />
          </ChartCard>
        </div>

        <div className="mt-4 grid gap-4 xl:grid-cols-2">
          <ChartCard title="Formas de pagamento">
            <SectionIntro
              title="Como você pagou"
              description="Entenda se seus gastos se concentram em Pix, débito, crédito, boleto ou dinheiro."
              helpText="Use esta seção para perceber se alguma forma de pagamento está pesando demais no mês."
            />
            <div className="h-64">
              {paymentRows.length && chartsReady ? (
                <ReportBarChart
                  data={paymentRows}
                  bars={[{ dataKey: "total", name: "Valor", fill: "#4F46E5" }]}
                  xAxisDataKey="label"
                  theme={chartTheme}
                  margin={{ left: 4, right: 12, bottom: 18 }}
                />
              ) : (
                <EmptyState title="Sem formas de pagamento" description="Cadastre despesas para ver esta comparação." icon={Wallet} />
              )}
            </div>
          </ChartCard>

          <ChartCard title="Evolução mensal">
            <SectionIntro
              title="Entradas, saídas e saldo"
              description="Acompanhe a evolução recente para identificar tendência do mês."
              helpText="A comparação usa os últimos meses disponíveis no dashboard."
            />
            <div className="h-64">
              {trendRows.length && chartsReady ? (
                <ReportBarChart
                  data={trendRows}
                  bars={[
                    { dataKey: "inflow", name: "Entradas", fill: "#18A957", radius: [4, 4, 0, 0] },
                    { dataKey: "outflow", name: "Saídas", fill: "#E14B5A", radius: [4, 4, 0, 0], animationDuration: 700 },
                    { dataKey: "net", name: "Saldo", fill: "#2F80ED", radius: [4, 4, 0, 0], animationDuration: 750 }
                  ]}
                  xAxisDataKey="label"
                  theme={chartTheme}
                  showLegend
                  margin={{ left: 4, right: 12, bottom: 18 }}
                />
              ) : (
                <EmptyState title="Sem evolução para mostrar" description="O histórico aparece quando houver movimentações ao longo dos meses." icon={TrendingUp} />
              )}
            </div>
          </ChartCard>
        </div>

        <section className="app-card mt-4 p-4">
          <SectionIntro
            title="Alertas principais"
            description="Pontos do mês que merecem revisão antes de fechar o planejamento."
            helpText="Alertas usam orçamento, categorias, parcelas e projeções do mês."
          />
          <div className="grid gap-2 md:grid-cols-2">
            {(report?.alerts || []).slice(0, 4).map((alert, index) => (
              <article className="interactive-list-item flex gap-3 rounded-app border border-line bg-surface/85 p-3 text-sm" key={`${alert.message}-${index}`}>
                <AlertTriangle className={alert.type === "danger" ? "mt-0.5 shrink-0 text-danger" : "mt-0.5 shrink-0 text-warning"} size={18} aria-hidden />
                <div>
                  <strong className="block">{alert.category}</strong>
                  <span className="text-muted">{alert.message}</span>
                </div>
              </article>
            ))}
            {!report?.alerts.length ? <p className="text-sm text-muted">Nenhum alerta relevante para este mês.</p> : null}
          </div>
        </section>

        <section className="app-card mt-4 p-4">
          <SectionIntro
            title="Orçamento por categoria"
            description="Veja os limites planejados e o quanto já foi usado em cada categoria."
            helpText="Se esta área estiver vazia, vá em Orçamento e crie limites para o mês."
          />
          <div className="mt-3 grid gap-2 md:grid-cols-2">
            {(report?.budget.items || []).map((item) => (
              <div key={item.id} className="rounded-app border border-line p-3 text-sm">
                <strong>{item.categoryName}</strong>
                <span className="block text-muted">{formatBRL(item.spent)} de {formatBRL(item.plannedAmount)}</span>
              </div>
            ))}
          </div>
          {!report?.budget.items.length ? (
            <EmptyState title="Nenhum orçamento no relatório" description="Crie limites por categoria para acompanhar o planejado contra o gasto." actionLabel="Criar orçamento" href="/orcamento" icon={FileText} />
          ) : null}
        </section>
      </div>
    </Shell>
  );
}
