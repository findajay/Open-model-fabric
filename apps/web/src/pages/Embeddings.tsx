import type { EmbeddingResponse } from "@omf/sdk";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Sigma } from "lucide-react";
import { useState } from "react";

import { ErrorAlert } from "@/components/ErrorAlert";
import { PageHeader } from "@/components/PageHeader";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Label, NativeSelect, Textarea } from "@/components/ui/input";
import { api } from "@/lib/api";
import { deploymentValue, deploymentsWith, parseDeploymentValue } from "@/lib/deployments";
import { ms } from "@/lib/format";

const EXAMPLES = [
  "The cat sat on the mat.",
  "A cat was sitting on the mat.",
  "Quarterly revenue grew by twelve percent.",
].join("\n");

/** Pairwise cosine similarity. Vectors need not be normalised. */
export function cosineMatrix(vectors: number[][]): number[][] {
  const norms = vectors.map((v) => Math.sqrt(v.reduce((s, x) => s + x * x, 0)));
  return vectors.map((a, i) =>
    vectors.map((b, j) => {
      const denominator = (norms[i] ?? 0) * (norms[j] ?? 0);
      if (!denominator) return 0;
      return a.reduce((s, x, k) => s + x * (b[k] ?? 0), 0) / denominator;
    }),
  );
}

function Matrix({ texts, result }: { texts: string[]; result: EmbeddingResponse }) {
  const matrix = cosineMatrix(result.data.map((d) => d.embedding));
  return (
    <div className="overflow-x-auto">
      <table className="text-xs" data-testid="similarity-matrix">
        <thead>
          <tr>
            <th />
            {texts.map((_, j) => (
              <th key={j} className="px-2 py-1 font-mono">
                #{j + 1}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {matrix.map((row, i) => (
            <tr key={i}>
              <th className="max-w-64 truncate px-2 py-1 text-left font-normal" title={texts[i]}>
                #{i + 1} {texts[i]}
              </th>
              {row.map((value, j) => (
                <td
                  key={j}
                  className="px-2 py-1 text-right font-mono"
                  style={{
                    backgroundColor: `color-mix(in oklab, var(--color-primary) ${Math.round(
                      Math.max(0, value) * 60,
                    )}%, transparent)`,
                  }}
                >
                  {value.toFixed(3)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function EmbeddingsPage() {
  const deployments = useQuery({ queryKey: ["deployments"], queryFn: () => api.deployments() });
  const options = deploymentsWith(deployments.data, "embeddings");
  const [selection, setSelection] = useState("");
  const [text, setText] = useState(EXAMPLES);
  const texts = text
    .split("\n")
    .map((t) => t.trim())
    .filter(Boolean);
  const run = useMutation({
    mutationFn: () => api.embeddings({ input: texts, ...parseDeploymentValue(selection) }),
  });

  return (
    <div className="p-6">
      <PageHeader
        title="Embeddings"
        description="Embed texts with any model that declares the embeddings capability and compare them by cosine similarity."
      />
      <ErrorAlert error={deployments.error} className="mb-4" />
      {deployments.isSuccess && options.length === 0 && (
        <p className="mb-4 text-sm text-muted-foreground">
          No available deployment declares <code>embeddings</code>. Add an embedding model (for
          example Ollama&apos;s nomic-embed-text) or declare one on an OpenAI-compatible provider.
        </p>
      )}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Sigma className="size-4" aria-hidden /> Compare texts
          </CardTitle>
          <CardDescription>One text per line (up to 256).</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <div>
            <Label htmlFor="embedding-model">Model</Label>
            <NativeSelect
              id="embedding-model"
              value={selection}
              onChange={(e) => setSelection(e.target.value)}
            >
              <option value="">Routed by policy (first embeddings model)</option>
              {options.map((d) => (
                <option key={d.id} value={deploymentValue(d)}>
                  {d.provider_id} / {d.provider_model}
                </option>
              ))}
            </NativeSelect>
          </div>
          <div>
            <Label htmlFor="embedding-texts">Texts</Label>
            <Textarea
              id="embedding-texts"
              value={text}
              onChange={(e) => setText(e.target.value)}
              className="min-h-28 font-mono text-xs"
            />
          </div>
          <div>
            <Button
              onClick={() => run.mutate()}
              disabled={run.isPending || texts.length === 0 || texts.length > 256}
            >
              Embed {texts.length} text{texts.length === 1 ? "" : "s"}
            </Button>
          </div>
          <ErrorAlert error={run.error} />
          {run.data && (
            <>
              <p className="font-mono text-xs text-muted-foreground" data-testid="embedding-meta">
                {run.data.provider_id}/{run.data.model} · {run.data.dimensions} dimensions ·{" "}
                {ms(run.data.timings.total_ms)}
              </p>
              <Matrix texts={texts} result={run.data} />
            </>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
