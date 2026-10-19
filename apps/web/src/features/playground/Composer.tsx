import { Braces, ImagePlus, Send, Square, X } from "lucide-react";
import { useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";

export interface Attachment {
  name: string;
  mediaType: string;
  dataBase64: string;
}

export interface ComposerCapabilities {
  vision: boolean;
  json: boolean;
}

const IMAGE_TYPES = ["image/png", "image/jpeg", "image/webp", "image/gif"];
const MAX_IMAGE_BYTES = 5 * 1024 * 1024;

function toBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",", 2)[1] ?? "");
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

/**
 * Message composer. Controls are driven by the selected model's declared capabilities:
 * image upload only appears for models that claim `vision_input`, JSON mode only for
 * `structured_output`. Nothing is offered that the capability registry does not declare.
 */
export function Composer({
  capabilities,
  streaming,
  disabled,
  onSend,
  onCancel,
}: {
  capabilities: ComposerCapabilities;
  streaming: boolean;
  disabled?: boolean;
  onSend: (message: { text: string; images: Attachment[]; json: boolean }) => void;
  onCancel: () => void;
}) {
  const [text, setText] = useState("");
  const [images, setImages] = useState<Attachment[]>([]);
  const [json, setJson] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  const effectiveImages = capabilities.vision ? images : [];
  const effectiveJson = capabilities.json && json;
  const canSend = !disabled && !streaming && (text.trim().length > 0 || effectiveImages.length > 0);

  async function addFiles(files: FileList | null) {
    setError(null);
    for (const file of Array.from(files ?? [])) {
      if (!IMAGE_TYPES.includes(file.type)) {
        setError(`${file.name}: only PNG, JPEG, WebP or GIF images are accepted`);
        continue;
      }
      if (file.size > MAX_IMAGE_BYTES) {
        setError(`${file.name}: images must be 5 MB or smaller`);
        continue;
      }
      const dataBase64 = await toBase64(file);
      setImages((prev) => [...prev, { name: file.name, mediaType: file.type, dataBase64 }]);
    }
  }

  function submit() {
    if (!canSend) return;
    onSend({ text: text.trim(), images: effectiveImages, json: effectiveJson });
    setText("");
    setImages([]);
  }

  return (
    <div className="rounded-lg border bg-card p-2 shadow-xs">
      {effectiveImages.length > 0 && (
        <div className="mb-2 flex flex-wrap gap-2" aria-label="Attached images">
          {effectiveImages.map((img, i) => (
            <span
              key={`${img.name}-${i}`}
              className="inline-flex items-center gap-1 rounded bg-muted px-2 py-1 text-xs"
            >
              {img.name}
              <button
                type="button"
                aria-label={`Remove ${img.name}`}
                onClick={() => setImages((prev) => prev.filter((_, j) => j !== i))}
              >
                <X className="size-3" />
              </button>
            </span>
          ))}
        </div>
      )}
      <Textarea
        aria-label="Message"
        placeholder="Send a message… (Enter to send, Shift+Enter for a new line)"
        value={text}
        disabled={disabled}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            submit();
          }
        }}
        className="min-h-20 resize-none border-0 shadow-none focus-visible:ring-0"
      />
      {error && <p className="px-2 text-xs text-status-bad">{error}</p>}
      <div className="flex items-center justify-between gap-2 pt-1">
        <div className="flex items-center gap-3">
          {capabilities.vision && (
            <>
              <input
                ref={fileInput}
                type="file"
                accept={IMAGE_TYPES.join(",")}
                multiple
                hidden
                data-testid="image-input"
                onChange={(e) => {
                  void addFiles(e.target.files);
                  e.target.value = "";
                }}
              />
              <Button
                type="button"
                variant="ghost"
                size="sm"
                onClick={() => fileInput.current?.click()}
                aria-label="Attach image"
              >
                <ImagePlus /> Image
              </Button>
            </>
          )}
          {capabilities.json && (
            <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <Switch checked={json} onCheckedChange={setJson} aria-label="JSON mode" />
              <Braces className="size-3.5" aria-hidden /> JSON mode
            </label>
          )}
        </div>
        {streaming ? (
          <Button type="button" variant="outline" size="sm" onClick={onCancel}>
            <Square /> Stop
          </Button>
        ) : (
          <Button type="button" size="sm" onClick={submit} disabled={!canSend}>
            <Send /> Send
          </Button>
        )}
      </div>
    </div>
  );
}
