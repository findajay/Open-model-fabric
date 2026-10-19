import type { ModelDeployment } from "@omf/sdk";
import { BadgeCheck, CircleHelp, Server } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { CAPABILITY_LABELS, EVIDENCE_LABELS } from "@/lib/capabilities";

const EVIDENCE_ICON = {
  verified: BadgeCheck,
  provider_reported: Server,
  declared: CircleHelp,
} as const;

export function CapabilityBadges({ deployment }: { deployment: ModelDeployment }) {
  const items = deployment.capabilities?.claims ?? [];
  if (items.length === 0)
    return <span className="text-xs text-muted-foreground">none declared</span>;
  return (
    <div className="flex flex-wrap gap-1">
      {items.map((c) => {
        const Icon = EVIDENCE_ICON[c.evidence];
        return (
          <Badge
            key={c.capability}
            variant={c.evidence === "declared" ? "outline" : "secondary"}
            title={`${CAPABILITY_LABELS[c.capability]}: ${EVIDENCE_LABELS[c.evidence]} (${c.source})`}
          >
            <Icon className="size-3" aria-hidden />
            {CAPABILITY_LABELS[c.capability]}
          </Badge>
        );
      })}
    </div>
  );
}

export function EvidenceLegend() {
  return (
    <div className="flex flex-wrap gap-3 text-xs text-muted-foreground">
      {(Object.keys(EVIDENCE_ICON) as (keyof typeof EVIDENCE_ICON)[]).map((k) => {
        const Icon = EVIDENCE_ICON[k];
        return (
          <span key={k} className="inline-flex items-center gap-1">
            <Icon className="size-3" aria-hidden /> {EVIDENCE_LABELS[k]}
          </span>
        );
      })}
    </div>
  );
}
