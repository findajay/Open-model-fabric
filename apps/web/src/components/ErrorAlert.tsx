import { OmfApiError } from "@omf/sdk";
import { AlertCircle } from "lucide-react";

export function describeError(error: unknown): {
  title: string;
  code?: string;
  requestId?: string | null;
} {
  if (error instanceof OmfApiError)
    return { title: error.message, code: error.code, requestId: error.requestId };
  if (error instanceof Error) return { title: error.message };
  return { title: String(error) };
}

export function ErrorAlert({ error, className = "" }: { error: unknown; className?: string }) {
  if (!error) return null;
  const { title, code, requestId } = describeError(error);
  return (
    <div
      role="alert"
      className={`flex gap-2 rounded-md border border-status-bad/40 bg-status-bad/10 p-3 text-sm ${className}`}
    >
      <AlertCircle className="mt-0.5 size-4 shrink-0 text-status-bad" aria-hidden />
      <div className="min-w-0">
        <div className="font-medium break-words">{title}</div>
        {(code || requestId) && (
          <div className="mt-0.5 font-mono text-xs text-muted-foreground">
            {code}
            {requestId ? ` · request ${requestId}` : ""}
          </div>
        )}
      </div>
    </div>
  );
}
