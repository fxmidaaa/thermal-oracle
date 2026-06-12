/** Минимальные UI-примитивы в стилистике shadcn/ui (тёмная тема, Tailwind),
 * без CLI-зависимости — при переезде на настоящий shadcn классы совместимы. */
import type { ButtonHTMLAttributes, ReactNode } from "react";

export function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return (
    <div className={`rounded-xl border border-zinc-800 bg-zinc-900 shadow-sm ${className}`}>
      {children}
    </div>
  );
}

export function CardHeader({ title, subtitle }: { title: string; subtitle?: string }) {
  return (
    <div className="px-5 pt-4 pb-1">
      <div className="text-sm font-medium text-zinc-400">{title}</div>
      {subtitle && <div className="text-xs text-zinc-500">{subtitle}</div>}
    </div>
  );
}

export function CardContent({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <div className={`px-5 pb-4 ${className}`}>{children}</div>;
}

const BADGE_VARIANTS: Record<string, string> = {
  ok: "bg-emerald-950 text-emerald-400 border-emerald-900",
  warn: "bg-amber-950 text-amber-400 border-amber-900",
  bad: "bg-red-950 text-red-400 border-red-900",
  muted: "bg-zinc-800 text-zinc-400 border-zinc-700",
};

export function Badge({ children, variant = "muted" }: { children: ReactNode; variant?: keyof typeof BADGE_VARIANTS }) {
  return (
    <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium ${BADGE_VARIANTS[variant]}`}>
      {children}
    </span>
  );
}

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: "default" | "outline" | "ghost";
}

const BUTTON_VARIANTS = {
  default: "bg-sky-600 text-white hover:bg-sky-500 disabled:bg-zinc-700",
  outline: "border border-zinc-700 text-zinc-300 hover:bg-zinc-800",
  ghost: "text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200",
};

export function Button({ variant = "default", className = "", ...props }: ButtonProps) {
  return (
    <button
      className={`inline-flex items-center justify-center rounded-md px-3 py-1.5 text-sm font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-60 ${BUTTON_VARIANTS[variant]} ${className}`}
      {...props}
    />
  );
}

/** Кнопка-переключатель для табов домена / периода. */
export function Toggle({ active, children, onClick }: { active: boolean; children: ReactNode; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className={`rounded-md px-2.5 py-1 text-xs font-medium transition-colors ${
        active ? "bg-zinc-700 text-zinc-100" : "text-zinc-500 hover:text-zinc-300"
      }`}
    >
      {children}
    </button>
  );
}

export function Alert({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="rounded-xl border border-amber-900/60 bg-amber-950/40 px-5 py-4">
      <div className="mb-1 flex items-center gap-2 text-sm font-semibold text-amber-300">
        <span aria-hidden>⚠️</span> {title}
      </div>
      <div className="text-sm text-amber-100/80">{children}</div>
    </div>
  );
}

export function Spinner({ label }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 text-sm text-zinc-500">
      <span className="size-3 animate-spin rounded-full border-2 border-zinc-600 border-t-zinc-300" />
      {label ?? "Загрузка…"}
    </div>
  );
}
