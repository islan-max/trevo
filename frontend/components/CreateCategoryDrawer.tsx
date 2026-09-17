"use client";

import { useId, useRef, useState } from "react";
import { X, Plus } from "@/components/icons";
import { Dialog } from "@/components/Dialog";
import { useDelayedPresence } from "@/lib/useDelayedPresence";
import type { Category, TransactionType } from "@/types/finance";

const categoryColors = [
  "#ef4444", // red
  "#f97316", // orange
  "#eab308", // yellow
  "#22c55e", // green
  "#06b6d4", // cyan
  "#3b82f6", // blue
  "#8b5cf6", // purple
  "#ec4899", // pink
  "#6b7280", // gray
];

const categoryIcons = [
  "🏪", "🚗", "🏠", "👕", "🍔", "💊", "📱", "✈️", "🎬", "💪", "📚", "🎮"
];

export type CreateCategoryInput = {
  name: string;
  type: TransactionType;
  color: string;
  icon: string;
};

type CreateCategoryDrawerProps = {
  open: boolean;
  onClose: () => void;
  onSubmit: (category: CreateCategoryInput) => Promise<Category | void>;
  defaultType?: TransactionType;
};

export function CreateCategoryDrawer({
  open,
  onClose,
  onSubmit,
  defaultType = "expense"
}: CreateCategoryDrawerProps) {
  const [form, setForm] = useState<CreateCategoryInput>({
    name: "",
    type: defaultType,
    color: categoryColors[0],
    icon: categoryIcons[0]
  });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const { shouldRender, state } = useDelayedPresence(open, 180);
  const titleId = useId();
  const nameInputRef = useRef<HTMLInputElement | null>(null);

  if (!shouldRender) return null;

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!form.name.trim()) {
      setError("Nome é obrigatório");
      return;
    }

    setLoading(true);
    setError("");
    try {
      await onSubmit(form);
      setForm({ name: "", type: defaultType, color: categoryColors[0], icon: categoryIcons[0] });
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Erro ao criar categoria");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="fixed inset-0 z-[220] flex items-center justify-center">
      {/* Blur background */}
      <div
        className={`absolute inset-0 bg-black/60 backdrop-blur-sm ${state === "open" ? "animate-overlay-in" : "animate-overlay-out"}`}
        onClick={onClose}
      />

      {/* Modal */}
      <Dialog
        busy={loading}
        className={`theme-surface relative mx-4 w-full max-w-md rounded-app border p-6 shadow-lift ${state === "open" ? "animate-pop-in" : "animate-pop-out"}`}
        initialFocusRef={nameInputRef}
        onClose={onClose}
        open={open}
        titleId={titleId}
      >
        <div className="flex items-center justify-between gap-4 mb-4">
          <h2 className="text-lg font-bold" id={titleId}>Criar categoria</h2>
          <button
            type="button"
            onClick={onClose}
            className="focus-ring theme-control inline-flex h-9 w-9 items-center justify-center rounded-app border text-muted transition hover:text-ink"
            aria-label="Fechar"
          >
            <X size={20} />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          <label className="block text-sm">
            <span className="font-semibold text-ink">Nome da categoria</span>
            <input
              type="text"
              className="field mt-1 w-full"
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              placeholder="Ex: Alimentação, Transporte"
              disabled={loading}
              ref={nameInputRef}
            />
          </label>

          <label className="block text-sm">
            <span className="font-semibold text-ink">Tipo</span>
            <div className="grid grid-cols-2 gap-2 mt-1">
              {(["expense", "income"] as const).map((type) => (
                <button
                  key={type}
                  type="button"
                  onClick={() => setForm({ ...form, type })}
                  className={`interactive-list-item p-3 rounded-app border-2 text-sm font-semibold transition ${
                    form.type === type
                      ? type === "expense"
                        ? "border-danger bg-danger/10 text-danger"
                        : "border-success bg-success/10 text-success"
                      : "theme-control border-line hover:border-leaf/50"
                  }`}
                  disabled={loading}
                >
                  {type === "expense" ? "Despesa" : "Entrada"}
                </button>
              ))}
            </div>
          </label>

          <label className="block text-sm">
            <span className="font-semibold text-ink">Cor</span>
            <div className="grid grid-cols-5 gap-2 mt-1">
              {categoryColors.map((color) => (
                <button
                  key={color}
                  type="button"
                  onClick={() => setForm({ ...form, color })}
                  className={`h-10 rounded-app border-2 transition hover:scale-105 ${
                    form.color === color
                      ? "border-ink scale-110"
                      : "border-line hover:border-ink"
                  }`}
                  style={{ backgroundColor: color }}
                  aria-label={`Selecionar cor ${color}`}
                  disabled={loading}
                />
              ))}
            </div>
          </label>

          <label className="block text-sm">
            <span className="font-semibold text-ink">Ícone</span>
            <div className="grid grid-cols-6 gap-2 mt-1">
              {categoryIcons.map((icon) => (
                <button
                  key={icon}
                  type="button"
                  onClick={() => setForm({ ...form, icon })}
                  className={`interactive-list-item h-10 text-xl rounded-app border-2 flex items-center justify-center transition ${
                    form.icon === icon
                      ? "scale-110 border-ink bg-surface/75"
                      : "border-line hover:border-leaf hover:bg-leaf/10"
                  }`}
                  aria-label={`Selecionar ícone ${icon}`}
                  disabled={loading}
                >
                  {icon}
                </button>
              ))}
            </div>
          </label>

          {error && (
            <p aria-live="assertive" className="feedback-message text-sm text-danger bg-danger/10 p-3 rounded-app" role="alert">
              {error}
            </p>
          )}

          <div className="flex gap-2">
            <button
              type="submit"
              className="btn-primary flex-1"
              disabled={loading}
            >
              <Plus size={16} />
              Criar categoria
            </button>
            <button
              type="button"
              onClick={onClose}
              className="btn-secondary flex-1"
              disabled={loading}
            >
              Cancelar
            </button>
          </div>
        </form>
      </Dialog>
    </div>
  );
}
