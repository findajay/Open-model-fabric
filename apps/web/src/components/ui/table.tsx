import type * as React from "react";

import { cn } from "@/lib/utils";

export function Table({ className, ...props }: React.TableHTMLAttributes<HTMLTableElement>) {
  return (
    <div className="w-full overflow-x-auto">
      <table className={cn("w-full caption-bottom text-sm", className)} {...props} />
    </div>
  );
}
export const THead = (p: React.HTMLAttributes<HTMLTableSectionElement>) => (
  <thead {...p} className={cn("[&_tr]:border-b", p.className)} />
);
export const TBody = (p: React.HTMLAttributes<HTMLTableSectionElement>) => (
  <tbody {...p} className={cn("[&_tr:last-child]:border-0", p.className)} />
);
export const TR = (p: React.HTMLAttributes<HTMLTableRowElement>) => (
  <tr {...p} className={cn("border-b transition-colors hover:bg-muted/50", p.className)} />
);
export const TH = (p: React.ThHTMLAttributes<HTMLTableCellElement>) => (
  <th
    {...p}
    className={cn(
      "h-8 px-2 text-left align-middle text-xs font-medium text-muted-foreground",
      p.className,
    )}
  />
);
export const TD = (p: React.TdHTMLAttributes<HTMLTableCellElement>) => (
  <td {...p} className={cn("px-2 py-1.5 align-middle", p.className)} />
);
