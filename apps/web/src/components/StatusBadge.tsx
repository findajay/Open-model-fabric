import { AlertTriangle, CheckCircle2, CircleDashed, CircleOff, XCircle } from "lucide-react";

import { Badge } from "@/components/ui/badge";

const MAP: Record<
  string,
  { variant: "good" | "warn" | "bad" | "secondary"; Icon: typeof CheckCircle2 }
> = {
  up: { variant: "good", Icon: CheckCircle2 },
  online: { variant: "good", Icon: CheckCircle2 },
  available: { variant: "good", Icon: CheckCircle2 },
  completed: { variant: "good", Icon: CheckCircle2 },
  ready: { variant: "good", Icon: CheckCircle2 },
  degraded: { variant: "warn", Icon: AlertTriangle },
  running: { variant: "warn", Icon: CircleDashed },
  pending: { variant: "warn", Icon: CircleDashed },
  streaming: { variant: "warn", Icon: CircleDashed },
  cancelled: { variant: "secondary", Icon: CircleOff },
  down: { variant: "bad", Icon: XCircle },
  offline: { variant: "bad", Icon: XCircle },
  failed: { variant: "bad", Icon: XCircle },
  unavailable: { variant: "bad", Icon: XCircle },
  disabled: { variant: "secondary", Icon: CircleOff },
  unknown: { variant: "secondary", Icon: CircleDashed },
};

/** Status is always icon + label, never color alone. */
export function StatusBadge({ status }: { status: string }) {
  const entry = MAP[status] ?? MAP.unknown!;
  const { variant, Icon } = entry;
  return (
    <Badge variant={variant}>
      <Icon className="size-3" aria-hidden />
      {status}
    </Badge>
  );
}
