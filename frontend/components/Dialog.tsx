"use client";

import { useEffect, useRef } from "react";
import type { ReactNode, RefObject } from "react";

type DialogProps = {
  open: boolean;
  onClose: () => void;
  titleId: string;
  describedById?: string;
  busy?: boolean;
  className: string;
  children: ReactNode;
  /** Elemento a focar ao abrir; sem ele, o foco inicial vai para o próprio painel. */
  initialFocusRef?: RefObject<HTMLElement | null>;
};

/**
 * Comportamento de diálogo acessível compartilhado por ImportConfirmDialog,
 * CreateCategoryDrawer e FinancialPlanningDrawer (UX-02): role="dialog",
 * aria-modal, foco entra no diálogo ao abrir, Tab/Shift+Tab circulam só
 * dentro do painel, Escape fecha (a menos que `busy`) e o foco volta pra quem
 * abriu o diálogo ao fechar.
 *
 * Cada chamador continua dono da própria aparência — backdrop, animação de
 * entrada/saída, posição na tela — via `className` e `children`. Este
 * componente só monta o `<div role="dialog">` e cuida de teclado/foco.
 */
export function Dialog({ open, onClose, titleId, describedById, busy = false, className, children, initialFocusRef }: DialogProps) {
  const dialogRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const previouslyFocused = document.activeElement as HTMLElement | null;
    (initialFocusRef?.current ?? dialogRef.current)?.focus();

    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape" && !busy) {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key !== "Tab" || !dialogRef.current) return;
      const focusables = dialogRef.current.querySelectorAll<HTMLElement>(
        'button:not([disabled]), [href], input:not([disabled]), select, textarea, [tabindex]:not([tabindex="-1"])'
      );
      if (!focusables.length) return;
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }

    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      previouslyFocused?.focus?.();
    };
  }, [open, busy, onClose, initialFocusRef]);

  return (
    <div
      aria-describedby={describedById}
      aria-labelledby={titleId}
      aria-modal="true"
      className={className}
      ref={dialogRef}
      role="dialog"
      tabIndex={-1}
    >
      {children}
    </div>
  );
}
