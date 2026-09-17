import Link from "next/link";
import { CircleHelp } from "@/components/icons";

export default function NotFound() {
  return (
    <div className="flex min-h-[70vh] items-center justify-center p-4">
      <div className="app-card w-full max-w-md p-6 text-center shadow-soft">
        <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-app border border-line bg-leaf/10 text-leaf">
          <CircleHelp aria-hidden size={22} />
        </div>
        <h1 className="mt-4 text-lg font-bold text-ink">Página não encontrada</h1>
        <p className="mt-2 text-sm text-muted">O endereço não existe ou foi movido. Volte para o início para continuar.</p>
        <div className="mt-5 flex justify-center">
          <Link className="btn-primary" href="/">
            Voltar ao início
          </Link>
        </div>
      </div>
    </div>
  );
}
