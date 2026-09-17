"use client";

import { useEffect, useId, useRef, useState } from "react";
import { AlertTriangle, Check, Layers3, X } from "@/components/icons";
import { Dialog } from "@/components/Dialog";
import { formatBRL } from "@/lib/format";
import type { CsvImportMode, CsvPreview } from "@/types/finance";

type ImportConfirmDialogProps = {
  open: boolean;
  preview: CsvPreview | null;
  busy?: boolean;
  onCancel: () => void;
  onConfirm: (mode: CsvImportMode) => void;
};

/**
 * Confirmação da importação: escolha entre mesclar e substituir.
 *
 * "Substituir" apaga lançamentos, então o diálogo mostra quantos e de quais
 * meses ANTES de confirmar, e a opção nasce desmarcada — o modo seguro é o
 * default. Um resumo do arquivo fica visível o tempo todo para a decisão não
 * depender de lembrar a tela anterior.
 */
export function ImportConfirmDialog({ open, preview, busy = false, onCancel, onConfirm }: ImportConfirmDialogProps) {
  const [mode, setMode] = useState<CsvImportMode>("merge");
  const titleId = useId();
  const descriptionId = useId();
  const confirmRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    if (open) setMode("merge");
  }, [open]);

  if (!open || !preview) return null;

  const importable = preview.validRows - preview.duplicateRows;
  const monthsLabel = preview.months?.map((entry) => entry.label).join(", ") || "—";
  const willDelete = preview.existingInMonths ?? 0;

  return (
    <div className="floating-layer inset-0 flex items-end justify-center bg-ink/60 p-0 backdrop-blur-sm sm:items-center sm:p-4">
      <Dialog
        busy={busy}
        className="animate-pop-in max-h-[92vh] w-full max-w-lg overflow-y-auto rounded-t-2xl border border-line bg-surface p-5 shadow-lift sm:rounded-app"
        describedById={descriptionId}
        initialFocusRef={confirmRef}
        onClose={onCancel}
        open={open}
        titleId={titleId}
      >
        <div className="flex items-start justify-between gap-3">
          <div>
            <h2 className="font-display text-lg font-bold text-ink" id={titleId}>
              Como importar estes dados?
            </h2>
            <p className="mt-1 text-sm text-muted" id={descriptionId}>
              {importable} {importable === 1 ? "movimentação" : "movimentações"} de {monthsLabel}, somando{" "}
              {formatBRL(preview.totalAmount || 0)}.
            </p>
          </div>
          <button
            aria-label="Fechar"
            className="focus-ring rounded-app p-2 text-muted transition hover:bg-surface-muted hover:text-ink"
            disabled={busy}
            onClick={onCancel}
            type="button"
          >
            <X size={18} />
          </button>
        </div>

        <fieldset className="mt-4 space-y-2">
          <legend className="sr-only">Modo de importação</legend>

          <label
            className={`flex cursor-pointer gap-3 rounded-app border p-3 transition ${
              mode === "merge" ? "border-leaf bg-leaf/10" : "border-line hover:border-line-strong"
            }`}
          >
            <input
              checked={mode === "merge"}
              className="mt-1 h-4 w-4 shrink-0 accent-leaf"
              name="import-mode"
              onChange={() => setMode("merge")}
              type="radio"
              value="merge"
            />
            <span className="min-w-0">
              <span className="flex items-center gap-2 font-semibold text-ink">
                <Layers3 aria-hidden size={16} />
                Mesclar com o que já existe
              </span>
              <span className="mt-1 block text-sm text-muted">
                Mantém seus lançamentos atuais e adiciona os novos. Repetições são ignoradas automaticamente.
                {preview.duplicateRows > 0 ? ` ${preview.duplicateRows} já existem e serão puladas.` : ""}
              </span>
            </span>
          </label>

          <label
            className={`flex cursor-pointer gap-3 rounded-app border p-3 transition ${
              mode === "replace" ? "border-danger bg-danger/10" : "border-line hover:border-line-strong"
            }`}
          >
            <input
              checked={mode === "replace"}
              className="mt-1 h-4 w-4 shrink-0 accent-danger"
              name="import-mode"
              onChange={() => setMode("replace")}
              type="radio"
              value="replace"
            />
            <span className="min-w-0">
              <span className="flex items-center gap-2 font-semibold text-ink">
                <AlertTriangle aria-hidden size={16} />
                Substituir os meses do arquivo
              </span>
              <span className="mt-1 block text-sm text-muted">
                Apaga o que existe em {monthsLabel} e deixa só o conteúdo do arquivo. Outros meses não são tocados.
              </span>
            </span>
          </label>
        </fieldset>

        {mode === "replace" && willDelete > 0 ? (
          <p
            className="mt-3 flex items-start gap-2 rounded-app border border-danger/40 bg-danger/10 p-3 text-sm text-ink"
            role="alert"
          >
            <AlertTriangle aria-hidden className="mt-0.5 shrink-0 text-danger" size={16} />
            <span>
              <strong>{willDelete}</strong> {willDelete === 1 ? "lançamento será apagado" : "lançamentos serão apagados"} em{" "}
              {monthsLabel}. Essa ação não pode ser desfeita.
            </span>
          </p>
        ) : null}

        <div className="mt-5 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
          <button className="btn-secondary" disabled={busy} onClick={onCancel} type="button">
            Cancelar
          </button>
          <button
            className="btn-primary"
            disabled={busy || importable <= 0}
            onClick={() => onConfirm(mode)}
            ref={confirmRef}
            type="button"
          >
            <Check aria-hidden size={16} />
            {busy ? "Importando..." : mode === "replace" ? "Substituir e importar" : "Mesclar e importar"}
          </button>
        </div>
      </Dialog>
    </div>
  );
}
