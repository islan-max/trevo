"use client";

import { FormEvent, useEffect, useId, useState } from "react";
import { WalletCards, X } from "@/components/icons";
import { Dialog } from "@/components/Dialog";
import { MoneyInput } from "@/components/MoneyInput";
import { useDelayedPresence } from "@/lib/useDelayedPresence";
import type { Settings } from "@/types/finance";

type PlanningValues = {
  monthlyIncome: number;
  reserveAmount: number;
  dailyGoal: number;
};

type FinancialPlanningDrawerProps = {
  open: boolean;
  settings: Settings | null;
  onClose: () => void;
  onSave: (values: PlanningValues) => Promise<void>;
};

export function FinancialPlanningDrawer({ open, settings, onClose, onSave }: FinancialPlanningDrawerProps) {
  const [form, setForm] = useState({
    monthlyIncome: 0,
    reserveAmount: 0,
    dailyGoal: 0
  });
  const [saving, setSaving] = useState(false);
  const { shouldRender, state } = useDelayedPresence(open, 180);
  const titleId = useId();

  useEffect(() => {
    if (!open) return;
    setForm({
      monthlyIncome: settings?.monthly_income ?? 0,
      reserveAmount: settings?.reserve_amount ?? 0,
      dailyGoal: settings?.daily_goal ?? 0
    });
  }, [open, settings]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSaving(true);
    try {
      await onSave({
        monthlyIncome: form.monthlyIncome,
        reserveAmount: form.reserveAmount,
        dailyGoal: form.dailyGoal
      });
      onClose();
    } finally {
      setSaving(false);
    }
  }

  if (!shouldRender) return null;

  return (
    <div className={`fixed inset-0 z-[220] bg-black/60 backdrop-blur-sm ${state === "open" ? "animate-overlay-in" : "animate-overlay-out"}`}>
      <Dialog
        busy={saving}
        className={`theme-surface absolute inset-x-0 bottom-0 max-h-[92vh] overflow-y-auto rounded-t-app border p-4 shadow-lift sm:left-auto sm:right-4 sm:top-4 sm:h-[calc(100vh-2rem)] sm:w-[420px] sm:rounded-app ${state === "open" ? "animate-pop-in" : "animate-pop-out"}`}
        onClose={onClose}
        open={open}
        titleId={titleId}
      >
        <div className="mb-4 flex items-start justify-between gap-3">
          <div>
            <p className="flex items-center gap-2 text-sm font-semibold text-leaf">
              <WalletCards size={18} />
              Ajustes rápidos
            </p>
            <h2 className="mt-1 text-xl font-black" id={titleId}>Editar planejamento</h2>
            <p className="mt-1 text-sm text-muted">Atualize o salário, a reserva planejada e a meta diária usada no Resumo.</p>
          </div>
          <button className="focus-ring theme-control h-9 w-9 rounded-app border" type="button" onClick={onClose} aria-label="Fechar">
            <X className="mx-auto" size={16} />
          </button>
        </div>

        <form onSubmit={submit} className="space-y-3">
          <label className="block text-sm">
            Salário base
            <MoneyInput className="field mt-1" value={form.monthlyIncome} onValueChange={(monthlyIncome) => setForm({ ...form, monthlyIncome })} />
          </label>
          <label className="block text-sm">
            Reserva planejada no mês
            <MoneyInput className="field mt-1" value={form.reserveAmount} onValueChange={(reserveAmount) => setForm({ ...form, reserveAmount })} />
          </label>
          <label className="block text-sm">
            Meta diária
            <MoneyInput className="field mt-1" value={form.dailyGoal} onValueChange={(dailyGoal) => setForm({ ...form, dailyGoal })} />
            <span className="mt-1 block text-xs text-muted">Use 0 para deixar o app recomendar a meta automaticamente.</span>
          </label>
          <div className="flex flex-col gap-2 pt-2 sm:flex-row">
            <button className="btn-primary" type="submit" disabled={saving}>
              {saving ? "Salvando..." : "Salvar planejamento"}
            </button>
            <button className="btn-secondary" type="button" onClick={onClose}>
              Cancelar
            </button>
          </div>
        </form>
      </Dialog>
    </div>
  );
}
