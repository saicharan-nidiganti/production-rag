import type { ButtonHTMLAttributes, ReactNode } from "react";
import { ApiError } from "../api/client";

type Variant = "primary" | "secondary" | "danger" | "ghost";

const VARIANTS: Record<Variant, string> = {
  primary:
    "bg-blue-700 text-white hover:bg-blue-800 disabled:bg-slate-300 disabled:text-slate-500 dark:disabled:bg-slate-700 dark:disabled:text-slate-400",
  secondary:
    "border border-slate-300 bg-white text-slate-800 hover:bg-slate-100 disabled:text-slate-400 dark:border-slate-600 dark:bg-slate-900 dark:text-slate-100 dark:hover:bg-slate-800",
  danger:
    "border border-red-300 bg-white text-red-700 hover:bg-red-50 dark:border-red-800 dark:bg-slate-900 dark:text-red-300 dark:hover:bg-red-950",
  ghost:
    "text-slate-700 hover:bg-slate-200 dark:text-slate-200 dark:hover:bg-slate-800",
};

export function Button({
  variant = "secondary",
  className = "",
  type = "button",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant }) {
  return (
    <button
      type={type}
      className={`inline-flex min-h-9 items-center justify-center gap-2 rounded-md px-3 py-1.5 text-sm font-medium disabled:cursor-not-allowed ${VARIANTS[variant]} ${className}`}
      {...props}
    />
  );
}

export function Spinner({ label }: { label: string }) {
  return (
    <span role="status" className="inline-flex items-center gap-2 text-sm text-slate-600 dark:text-slate-300">
      <span
        aria-hidden="true"
        className="size-4 animate-spin rounded-full border-2 border-slate-300 border-t-blue-700 motion-reduce:animate-none dark:border-slate-600 dark:border-t-blue-400"
      />
      {label}
    </span>
  );
}

type Tone = "error" | "warning" | "info";

const TONES: Record<Tone, string> = {
  error: "border-red-300 bg-red-50 text-red-900 dark:border-red-800 dark:bg-red-950 dark:text-red-100",
  warning:
    "border-amber-300 bg-amber-50 text-amber-900 dark:border-amber-700 dark:bg-amber-950 dark:text-amber-100",
  info: "border-slate-300 bg-white text-slate-800 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100",
};

export function Notice({
  tone = "info",
  title,
  children,
  action,
}: {
  tone?: Tone;
  title?: string;
  children?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div
      role={tone === "error" ? "alert" : "status"}
      className={`flex flex-wrap items-start justify-between gap-3 rounded-md border px-4 py-3 text-sm ${TONES[tone]}`}
    >
      <div>
        {title && <p className="font-semibold">{title}</p>}
        {children}
      </div>
      {action}
    </div>
  );
}

/** What went wrong, in the server's own words, with the id to quote when asking for help. */
export function ErrorNotice({ error, action }: { error: unknown; action?: ReactNode }) {
  const api = error instanceof ApiError ? error : null;
  const message = error instanceof Error ? error.message : "Something went wrong.";
  return (
    <Notice tone="error" title={titleFor(api)} action={action}>
      <p>{message}</p>
      {api?.retryAfter && <p>Try again in {api.retryAfter} seconds.</p>}
      {api?.requestId && (
        <p className="mt-1 text-xs opacity-80">
          Request id: <code>{api.requestId}</code>
        </p>
      )}
    </Notice>
  );
}

function titleFor(error: ApiError | null): string {
  if (!error) return "Something went wrong";
  if (error.status === 0) return "Can't reach the server";
  if (error.status === 429) return "Too many requests";
  if (error.status === 503) return "The model is unavailable right now";
  if (error.status >= 500) return "The server had a problem";
  return "That didn't work";
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function formatTime(iso: string): string {
  return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}
