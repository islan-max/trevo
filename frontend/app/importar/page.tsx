"use client";

import { ChangeEvent, FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, Check, ChevronDown, FileSpreadsheet, FileUp, ListChecks, ShieldCheck, Wand2 } from "@/components/icons";
import { EmptyState } from "@/components/EmptyState";
import { FeedbackMessage } from "@/components/FeedbackMessage";
import { PageHeader } from "@/components/PageHeader";
import { SectionIntro } from "@/components/SectionIntro";
import { Shell } from "@/components/Shell";
import { ImportConfirmDialog } from "@/components/ImportConfirmDialog";
import { api } from "@/lib/api";
import { formatBRL } from "@/lib/format";
import { useAuthToken } from "@/lib/useAuthToken";
import type { Category, CsvPreview, CsvUpload, Transaction } from "@/types/finance";

type Mapping = {
  date: string;
  description: string;
  value: string;
  type: string;
  category: string;
  account: string;
  time: string;
};
type ImportResult = { imported: number; duplicates: number; invalidRows: number; transactions: Transaction[] };
type StepKey = "upload" | "mapping" | "preview" | "confirm" | "categorize";

const steps: Array<{ key: StepKey; label: string }> = [
  { key: "upload", label: "Enviar arquivo" },
  { key: "mapping", label: "Mapear colunas" },
  { key: "preview", label: "Revisar prévia" },
  { key: "confirm", label: "Confirmar importação" },
  { key: "categorize", label: "Categorizar" }
];

function getStepIndex(upload: CsvUpload | null, preview: CsvPreview | null, result: ImportResult | null) {
  if (result) return 4;
  if (preview) return 3;
  if (upload) return 1;
  return 0;
}

function guessMapping(columns: string[]): Mapping {
  // A coluna de data não pode ser roubada por "data de vencimento" nem por uma
  // coluna de hora; por isso cada padrão é testado na ordem do mais específico.
  const find = (...patterns: RegExp[]) => {
    for (const pattern of patterns) {
      const match = columns.find((column) => pattern.test(column));
      if (match) return match;
    }
    return "";
  };

  return {
    date: find(/^data$/i, /data.*(lan[çc]|compra|movi|transa)/i, /\bdate\b/i, /data/i),
    description: find(/descri/i, /hist[óo]ric/i, /memo/i, /detalhe/i, /desc/i),
    value: find(/^valor$/i, /valor/i, /amount/i, /quantia/i, /value/i),
    type: find(/^tipo$/i, /tipo.*(lan[çc]|transa|movi)/i, /natureza/i, /\btype\b/i),
    category: find(/^categoria$/i, /categoria/i, /classific/i, /category/i),
    account: find(/^conta$/i, /conta/i, /origem/i, /banco/i, /carteira/i, /account/i),
    time: find(/^hora$/i, /hor[áa]rio/i, /\btime\b/i)
  };
}

function SelectField({
  helper,
  label,
  onChange,
  options,
  required = false,
  value
}: {
  helper?: string;
  label: string;
  onChange: (value: string) => void;
  options: string[];
  required?: boolean;
  value: string;
}) {
  return (
    <label className="block rounded-app border border-line bg-surface/90 p-3 text-sm shadow-sm">
      <span className="font-semibold text-ink">{label}</span>
      {helper ? <span className="mt-1 block text-xs text-muted">{helper}</span> : null}
      <span className="relative mt-3 block">
        <select
          className="focus-ring min-h-11 w-full appearance-none rounded-app border border-line bg-surface px-3 py-2 pr-10 text-sm shadow-sm"
          onChange={(event) => onChange(event.target.value)}
          required={required}
          value={value}
        >
          <option value="">Selecione a coluna</option>
          {options.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>
        <ChevronDown className="pointer-events-none absolute right-3 top-3 text-muted" size={16} aria-hidden />
      </span>
    </label>
  );
}

function Stepper({ activeIndex }: { activeIndex: number }) {
  return (
    <ol className="grid gap-2 sm:grid-cols-5" aria-label="Etapas da importação">
      {steps.map((step, index) => {
        const done = index < activeIndex;
        const active = index === activeIndex;
        return (
          <li
            aria-current={active ? "step" : undefined}
            className={`rounded-app border p-3 text-sm shadow-sm transition ${
              done ? "border-success/25 bg-success/10 text-ink" : active ? "border-leaf/40 bg-surface text-ink" : "border-line bg-surface/70 text-muted"
            }`}
            key={step.key}
          >
            <span className={`mb-2 flex h-7 w-7 items-center justify-center rounded-app text-xs font-black ${done ? "bg-success text-white" : active ? "is-selected" : "bg-ink/5 text-muted"}`}>
              {done ? <Check size={14} aria-hidden /> : index + 1}
            </span>
            <span className="block font-semibold">{step.label}</span>
          </li>
        );
      })}
    </ol>
  );
}

function StatCard({ label, value, tone = "neutral" }: { label: string; tone?: "neutral" | "good" | "warning" | "danger"; value: string | number }) {
  const toneClass = {
    neutral: "bg-surface text-ink",
    good: "bg-success/10 text-ink",
    warning: "bg-warning/15 text-ink",
    danger: "bg-danger/10 text-ink"
  };
  return (
    <div className={`rounded-app border border-line p-3 shadow-sm ${toneClass[tone]}`}>
      <span className="text-xs font-semibold uppercase tracking-normal text-muted">{label}</span>
      <strong className="metric-number mt-1 block text-xl">{value}</strong>
    </div>
  );
}

export default function ImportarPage() {
  const token = useAuthToken();
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [upload, setUpload] = useState<CsvUpload | null>(null);
  const [preview, setPreview] = useState<CsvPreview | null>(null);
  const [importResult, setImportResult] = useState<ImportResult | null>(null);
  const [categories, setCategories] = useState<Category[]>([]);
  const [mapping, setMapping] = useState<Mapping>({ date: "", description: "", value: "", type: "", category: "", account: "", time: "" });
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [message, setMessage] = useState("");
  const [selectedFileName, setSelectedFileName] = useState("");
  const [busyState, setBusyState] = useState<"upload" | "preview" | "confirm" | "rule" | "categorize" | "">("");

  // UX-05: handlePreview/handleConfirm/categorizeTransaction já tratam seus
  // próprios erros internamente (setMessage), mas os handlers de evento que os
  // chamam ainda precisam de um .catch — sem ele, uma falha inesperada fora do
  // try/catch interno virava só um console.error, sem nada visível na tela.
  function reportUnexpectedError(err: unknown) {
    setMessage(err instanceof Error ? err.message : "Falha inesperada.");
  }

  useEffect(() => {
    if (!token) return;
    api.bootstrap(token, new Date().toISOString().slice(0, 7))
      .then((boot) => setCategories(boot.categories))
      .catch((err) => setMessage(err instanceof Error ? err.message : "Falha ao carregar categorias."));
  }, [token]);

  const columns = upload?.columns || [];
  const mappingReady = useMemo(() => Boolean(mapping.date && mapping.description && mapping.value), [mapping]);
  const activeStep = getStepIndex(upload, preview, importResult);
  const importableRows = preview ? Math.max(preview.validRows - preview.duplicateRows, 0) : 0;

  async function uploadFile(file: File) {
    if (!token) return;
    if (!file.name.toLowerCase().endsWith(".csv")) {
      setMessage("Envie um arquivo com extensão .csv.");
      return;
    }
    setBusyState("upload");
    setMessage("");
    try {
      const result = await api.uploadCsv(token, file);
      setSelectedFileName(file.name);
      setUpload(result);
      setPreview(null);
      setImportResult(null);
      setMapping(guessMapping(result.columns));
      setMessage(`${result.totalRows} linhas encontradas. Agora confira o mapeamento das colunas.`);
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "Falha ao enviar arquivo.");
    } finally {
      setBusyState("");
    }
  }

  function handleFileChange(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (file) uploadFile(file).catch((err) => setMessage(err instanceof Error ? err.message : "Falha ao enviar arquivo."));
    event.target.value = "";
  }

  async function handlePreview() {
    if (!token || !upload || !mappingReady) return;
    setBusyState("preview");
    setMessage("");
    try {
      const result = await api.previewCsv(token, { importToken: upload.importToken, mapping: { ...mapping, type: mapping.type || null } });
      setPreview(result);
      setImportResult(null);
      setMessage("Prévia gerada. Revise os dados antes de confirmar.");
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "Falha ao gerar prévia.");
    } finally {
      setBusyState("");
    }
  }

  async function handleConfirm(mode: "merge" | "replace" = "merge") {
    if (!token || !upload || !preview || !mappingReady) return;
    setBusyState("confirm");
    setMessage("");
    try {
      const result = await api.confirmCsv(token, {
        importToken: upload.importToken,
        mapping: {
          ...mapping,
          type: mapping.type || null,
          category: mapping.category || null,
          account: mapping.account || null,
          time: mapping.time || null
        },
        mode
      });
      setConfirmOpen(false);
      setImportResult(result);
      setUpload(null);
      setPreview(null);
      setMessage(`${result.imported} movimentações importadas, ${result.duplicates} duplicatas ignoradas e ${result.invalidRows} linhas com erro.`);
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "Falha ao confirmar importação.");
    } finally {
      setBusyState("");
    }
  }

  async function categorizeTransaction(transaction: Transaction, categoryId: string) {
    if (!token || !categoryId) return;
    setBusyState("categorize");
    try {
      const updated = await api.updateTransaction(token, transaction.id, { categoryId: Number(categoryId) });
      setImportResult((current) => current ? {
        ...current,
        transactions: current.transactions.map((item) => item.id === transaction.id ? { ...item, category_id: updated.category_id, category_name: updated.category_name } : item)
      } : current);
      setMessage("Categoria atualizada.");
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "Falha ao categorizar.");
    } finally {
      setBusyState("");
    }
  }

  async function createRule(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!token) return;
    const form = new FormData(event.currentTarget);
    setBusyState("rule");
    try {
      await api.createRule(token, {
        pattern: String(form.get("pattern")),
        categoryId: Number(form.get("categoryId")),
        paymentMethod: String(form.get("paymentMethod") || "csv_import")
      });
      event.currentTarget.reset();
      setMessage("Regra criada. Ela será aplicada nas próximas importações.");
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "Falha ao criar regra.");
    } finally {
      setBusyState("");
    }
  }

  return (
    <Shell>
      <div className="mx-auto max-w-6xl px-4 py-5 sm:py-6">
        <PageHeader
          description="Você pode enviar um arquivo CSV do seu banco. O Trevo lê as movimentações, mostra uma prévia e você decide o que importar."
          helpText="Importe um extrato em CSV para cadastrar movimentações mais rápido. Você revisa tudo antes de confirmar."
          icon={FileUp}
          title="Importe seu extrato para cadastrar movimentações mais rápido."
        />

        <div className="mb-4 grid gap-3 md:grid-cols-3">
          {[
            "O arquivo não fica salvo permanentemente.",
            "Você revisa tudo antes de confirmar.",
            "Movimentações repetidas são detectadas automaticamente."
          ].map((notice) => (
            <div className="flex gap-3 rounded-app border border-line bg-surface/90 p-3 text-sm shadow-sm" key={notice}>
              <ShieldCheck className="mt-0.5 shrink-0 text-leaf" size={18} aria-hidden />
              <span>{notice}</span>
            </div>
          ))}
        </div>

        <Stepper activeIndex={activeStep} />

        <FeedbackMessage message={message} className="mt-4" />

        <section className="mt-4 grid gap-4 lg:grid-cols-[1fr_0.95fr]">
          <div className="app-card p-4">
            <SectionIntro
              title="1. Enviar arquivo"
              description="Envie um arquivo .CSV para começar. O tamanho máximo é validado pelo backend."
              helpText="Use um extrato CSV exportado pelo banco ou uma planilha com data, descrição e valor."
            />

            {!upload && !importResult ? (
              <EmptyState
                title="Envie um arquivo .CSV para começar."
                description="Depois do envio, o Trevo mostra as colunas encontradas e guia você pelo mapeamento."
                actionLabel={busyState === "upload" ? "Enviando..." : "Selecionar arquivo CSV"}
                onAction={() => fileInputRef.current?.click()}
                icon={FileSpreadsheet}
              />
            ) : null}

            {!upload && importResult ? (
              <div className="rounded-app border border-success/25 bg-success/10 p-4 text-sm">
                <strong className="block text-ink">Importação concluída.</strong>
                <p className="mt-1 text-muted">As movimentações importadas aparecem na etapa de categorização. Você pode selecionar outro CSV quando quiser.</p>
                <button className="btn-secondary mt-4" type="button" onClick={() => fileInputRef.current?.click()}>
                  Selecionar outro CSV
                </button>
              </div>
            ) : null}

            <input
              accept=".csv,text/csv"
              className="sr-only"
              onChange={handleFileChange}
              ref={fileInputRef}
              type="file"
            />

            {upload ? (
              <div className="grid gap-3 sm:grid-cols-3">
                <StatCard label="Arquivo" value={upload.filename || selectedFileName} />
                <StatCard label="Linhas" value={upload.totalRows} tone="good" />
                <StatCard label="Colunas" value={upload.columns.length} />
                <div className="sm:col-span-3">
                  <p className="text-sm font-semibold">Colunas encontradas</p>
                  <div className="mt-2 flex flex-wrap gap-2">
                    {upload.columns.map((column) => (
                      <span className="rounded-app border border-line bg-surface px-3 py-1 text-xs font-semibold text-muted" key={column}>
                        {column}
                      </span>
                    ))}
                  </div>
                  <button className="btn-secondary mt-4" type="button" onClick={() => fileInputRef.current?.click()}>
                    Trocar arquivo
                  </button>
                </div>
              </div>
            ) : null}
          </div>

          <div className="app-card p-4">
            <SectionIntro
              title="2. Mapear colunas"
              description="Escolha quais colunas do arquivo representam data, descrição e valor."
              helpText="Tipo é opcional. Quando não houver coluna de tipo, o Trevo tenta inferir entrada ou despesa pelo valor."
            />

            {upload ? (
              <>
                <div className="grid gap-3 sm:grid-cols-2">
                  <SelectField label="Data" helper="Ex: 03/06/2026" options={columns} required value={mapping.date} onChange={(date) => setMapping({ ...mapping, date })} />
                  <SelectField label="Descrição" helper="Nome que aparecerá na movimentação" options={columns} required value={mapping.description} onChange={(description) => setMapping({ ...mapping, description })} />
                  <SelectField label="Valor" helper="Pode ser positivo ou negativo" options={columns} required value={mapping.value} onChange={(value) => setMapping({ ...mapping, value })} />
                  <SelectField label="Tipo, se existir" helper="Entrada, saída, crédito ou débito" options={columns} value={mapping.type || ""} onChange={(type) => setMapping({ ...mapping, type })} />
                  <SelectField label="Categoria, se existir" helper="Casa com suas categorias pelo nome" options={columns} value={mapping.category || ""} onChange={(category) => setMapping({ ...mapping, category })} />
                  <SelectField label="Conta ou origem, se existir" helper="Banco, carteira ou forma de pagamento" options={columns} value={mapping.account || ""} onChange={(account) => setMapping({ ...mapping, account })} />
                  <SelectField label="Hora, se estiver em coluna separada" helper="Quando a data não traz o horário" options={columns} value={mapping.time || ""} onChange={(time) => setMapping({ ...mapping, time })} />
                </div>
                <button className="btn-primary mt-4" type="button" onClick={() => handlePreview().catch(reportUnexpectedError)} disabled={!mappingReady || busyState === "preview"}>
                  <ListChecks size={16} aria-hidden />
                  {busyState === "preview" ? "Gerando prévia..." : "Revisar prévia"}
                </button>
              </>
            ) : (
              <p className="rounded-app border border-dashed border-line bg-surface/70 p-4 text-sm text-muted">Selecione um CSV para liberar o mapeamento.</p>
            )}
          </div>
        </section>

        <section className="app-card mt-4 p-4">
          <SectionIntro
            title="3. Revisar prévia"
            description="Confira primeiras linhas válidas, erros e duplicatas antes de importar."
            helpText="Nada é salvo nessa etapa. A importação só acontece quando você confirmar."
          />

          {preview ? (
            <>
              <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
                <StatCard label="Linhas válidas" value={preview.validRows} tone="good" />
                <StatCard label="Com erro" value={preview.invalidRows} tone={preview.invalidRows ? "danger" : "neutral"} />
                <StatCard label="Duplicatas" value={preview.duplicateRows} tone={preview.duplicateRows ? "warning" : "neutral"} />
                <StatCard label="Serão importadas" value={importableRows} tone="good" />
              </div>

              <div className="mt-4 grid gap-4 xl:grid-cols-[1.2fr_0.8fr]">
                <div>
                  <h3 className="text-sm font-semibold">Primeiras linhas válidas</h3>
                  <div className="mt-2 divide-y divide-line rounded-app border border-line bg-surface">
                    {preview.preview.map((row) => (
                      <div key={row.duplicateHash} className="flex items-center justify-between gap-3 p-3">
                        <div className="min-w-0">
                          <strong className="block truncate text-sm">{row.title}</strong>
                          <span className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs text-muted">
                            <span>{new Date(`${row.transactionDate}T12:00:00`).toLocaleDateString("pt-BR")}</span>
                            {row.time ? <span aria-label={`Hora ${row.time}`}>{row.time}</span> : null}
                            <span aria-hidden>·</span>
                            <span>{row.monthLabel || row.detectedMonth}</span>
                            <span aria-hidden>·</span>
                            <span className={row.type === "income" ? "text-success" : "text-danger"}>{row.type === "income" ? "Entrada" : "Despesa"}</span>
                            {row.categoryName ? (
                              <>
                                <span aria-hidden>·</span>
                                <span className="rounded bg-surface-muted px-1.5 py-0.5">{row.categoryName}</span>
                              </>
                            ) : null}
                            {row.account ? (
                              <>
                                <span aria-hidden>·</span>
                                <span>{row.account}</span>
                              </>
                            ) : null}
                          </span>
                        </div>
                        <span className={row.type === "income" ? "font-semibold text-success" : "font-semibold text-danger"}>{formatBRL(row.amount)}</span>
                      </div>
                    ))}
                    {!preview.preview.length ? <p className="p-3 text-sm text-muted">Nenhuma linha válida encontrada.</p> : null}
                  </div>
                </div>

                <div className="grid gap-3">
                  <div className="rounded-app border border-line bg-surface p-3">
                    <h3 className="flex items-center gap-2 text-sm font-semibold">
                      <AlertTriangle size={16} className="text-warning" aria-hidden />
                      Linhas com erro
                    </h3>
                    <div className="mt-2 space-y-2">
                      {preview.errors.map((error) => (
                        <p key={`${error.line}-${error.detail}`} className="rounded-app bg-danger/10 p-2 text-sm text-danger">Linha {error.line}: {error.detail}</p>
                      ))}
                      {!preview.errors.length ? <p className="text-sm text-muted">Nenhum erro encontrado.</p> : null}
                    </div>
                  </div>

                  <div className="rounded-app border border-line bg-surface p-3">
                    <h3 className="text-sm font-semibold">Duplicatas detectadas</h3>
                    <div className="mt-2 space-y-2">
                      {preview.duplicates.map((row) => (
                        <p key={row.duplicateHash} className="rounded-app bg-warning/15 p-2 text-sm">Linha {row.line}: {row.title}</p>
                      ))}
                      {!preview.duplicates.length ? <p className="text-sm text-muted">Nenhuma movimentação repetida encontrada.</p> : null}
                    </div>
                  </div>
                </div>
              </div>

              <div className="mt-4 flex flex-wrap items-center gap-2">
                <button className="btn-primary" type="button" onClick={() => setConfirmOpen(true)} disabled={busyState === "confirm" || importableRows <= 0}>
                  Revisar e importar
                </button>
                <p className="text-sm text-muted">Você escolhe entre mesclar ou substituir antes de qualquer gravação.</p>
              </div>
            </>
          ) : (
            <EmptyState
              title="Prévia ainda não gerada"
              description="Mapeie as colunas e revise a prévia antes de confirmar qualquer importação."
              actionLabel={upload ? "Revisar prévia" : undefined}
              onAction={upload ? () => handlePreview().catch(reportUnexpectedError) : undefined}
              icon={ListChecks}
            />
          )}
        </section>

        <section className="app-card mt-4 p-4">
          <SectionIntro
            title="5. Categorizar"
            description="Depois da importação, revise as movimentações criadas e crie regras para acelerar próximas importações."
            helpText="A automação por regras já vale para próximas importações. Sugestões inteligentes por similaridade ficam preparadas como melhoria futura."
            action={<Wand2 size={18} className="text-leaf" aria-hidden />}
          />

          {importResult ? (
            <div className="grid gap-4 xl:grid-cols-[1.2fr_0.8fr]">
              <div>
                <div className="grid gap-3 sm:grid-cols-3">
                  <StatCard label="Importadas" value={importResult.imported} tone="good" />
                  <StatCard label="Duplicatas ignoradas" value={importResult.duplicates} tone={importResult.duplicates ? "warning" : "neutral"} />
                  <StatCard label="Erros" value={importResult.invalidRows} tone={importResult.invalidRows ? "danger" : "neutral"} />
                </div>

                <div className="mt-3 divide-y divide-line rounded-app border border-line bg-surface">
                  {importResult.transactions.map((transaction) => (
                    <div className="grid gap-3 p-3 md:grid-cols-[1fr_220px]" key={transaction.id}>
                      <div className="min-w-0">
                        <strong className="block truncate text-sm">{transaction.title}</strong>
                        <span className="text-xs text-muted">{transaction.transaction_date} / {formatBRL(transaction.amount)} / {transaction.type === "income" ? "Entrada" : "Despesa"}</span>
                      </div>
                      <select
                        className="field"
                        disabled={busyState === "categorize"}
                        onChange={(event) => categorizeTransaction(transaction, event.target.value).catch(reportUnexpectedError)}
                        value={transaction.category_id ? String(transaction.category_id) : ""}
                      >
                        <option value="">Categorizar</option>
                        {categories.filter((category) => category.type === transaction.type).map((category) => (
                          <option key={category.id} value={category.id}>{category.name}</option>
                        ))}
                      </select>
                    </div>
                  ))}
                  {!importResult.transactions.length ? <p className="p-3 text-sm text-muted">Nenhuma nova movimentação foi importada.</p> : null}
                </div>
              </div>

              <form onSubmit={createRule} className="rounded-app border border-line bg-surface p-3">
                <h3 className="text-sm font-semibold">Sempre categorizar descrições parecidas como X</h3>
                <p className="mt-1 text-sm text-muted">Crie uma regra por palavra-chave. Exemplo: descrições com “ifood” entram em Alimentação.</p>
                <div className="mt-3 grid gap-3">
                  <input className="field" name="pattern" placeholder="Ex: ifood, uber, netflix" required />
                  <select className="field" name="categoryId" required>
                    <option value="">Categoria</option>
                    {categories.map((category) => <option key={category.id} value={category.id}>{category.name}</option>)}
                  </select>
                  <select className="field" name="paymentMethod">
                    <option value="csv_import">CSV</option>
                    <option value="pix">Pix</option>
                    <option value="debito">Débito</option>
                    <option value="credito">Crédito</option>
                  </select>
                  <button className="btn-secondary" type="submit" disabled={busyState === "rule"}>{busyState === "rule" ? "Criando..." : "Criar regra"}</button>
                </div>
              </form>
            </div>
          ) : (
            <p className="rounded-app border border-dashed border-line bg-surface/70 p-4 text-sm text-muted">As movimentações importadas aparecerão aqui após a confirmação.</p>
          )}
        </section>
      </div>
      <ImportConfirmDialog
        busy={busyState === "confirm"}
        onCancel={() => setConfirmOpen(false)}
        onConfirm={(mode) => {
          handleConfirm(mode).catch(reportUnexpectedError);
        }}
        open={confirmOpen}
        preview={preview}
      />
    </Shell>
  );
}
