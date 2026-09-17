import { AlertCircle, CheckCircle2, Info, LoaderCircle } from "@/components/icons";

type FeedbackTone = "success" | "error" | "info" | "loading";

type FeedbackMessageProps = {
  message: string;
  tone?: FeedbackTone | "auto";
  className?: string;
};

const toneClass: Record<FeedbackTone, string> = {
  success: "border-success/25 bg-success/10 text-ink",
  error: "border-danger/25 bg-danger/10 text-ink",
  info: "border-leaf/25 bg-leaf/10 text-ink",
  loading: "border-info/25 bg-info/10 text-ink"
};

const iconClass: Record<FeedbackTone, string> = {
  success: "text-success",
  error: "text-danger",
  info: "text-leaf",
  loading: "text-info"
};

function iconForTone(tone: FeedbackTone) {
  if (tone === "success") return CheckCircle2;
  if (tone === "error") return AlertCircle;
  if (tone === "loading") return LoaderCircle;
  return Info;
}

function inferTone(message: string): FeedbackTone {
  const normalized = message.toLowerCase();
  if (
    normalized.includes("falha") ||
    normalized.includes("erro") ||
    normalized.includes("expirada") ||
    normalized.includes("maior que zero") ||
    normalized.includes("deve ") ||
    normalized.includes("envie ")
  ) {
    return "error";
  }
  if (
    normalized.includes("salv") ||
    normalized.includes("atualiz") ||
    normalized.includes("criad") ||
    normalized.includes("exclu") ||
    normalized.includes("removid") ||
    normalized.includes("importad") ||
    normalized.includes("conclu") ||
    normalized.includes("copiad")
  ) {
    return "success";
  }
  return "info";
}

export function FeedbackMessage({ message, tone = "auto", className = "" }: FeedbackMessageProps) {
  if (!message) return null;

  const resolvedTone = tone === "auto" ? inferTone(message) : tone;
  const Icon = iconForTone(resolvedTone);
  const role = resolvedTone === "error" ? "alert" : "status";

  return (
    <div
      aria-live={resolvedTone === "error" ? "assertive" : "polite"}
      className={`feedback-message mb-4 flex items-start gap-3 rounded-app border p-3 text-sm shadow-soft ${toneClass[resolvedTone]} ${className}`}
      role={role}
    >
      <Icon className={`mt-0.5 shrink-0 ${iconClass[resolvedTone]} ${resolvedTone === "loading" ? "animate-spin" : ""}`} size={18} aria-hidden />
      <span className="min-w-0 flex-1">{message}</span>
    </div>
  );
}
