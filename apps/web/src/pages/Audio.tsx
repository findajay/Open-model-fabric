import { useMutation, useQuery } from "@tanstack/react-query";
import { Mic, Square, Volume2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { ErrorAlert } from "@/components/ErrorAlert";
import { PageHeader } from "@/components/PageHeader";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Label, NativeSelect, Textarea } from "@/components/ui/input";
import { api } from "@/lib/api";
import { deploymentValue, deploymentsWith, parseDeploymentValue } from "@/lib/deployments";
import { ms } from "@/lib/format";

export const AUDIO_ACCEPT = [
  "audio/wav",
  "audio/x-wav",
  "audio/mpeg",
  "audio/mp4",
  "audio/x-m4a",
  "audio/ogg",
  "audio/webm",
  "audio/flac",
].join(",");

function ModelSelect({
  id,
  value,
  onChange,
  options,
}: {
  id: string;
  value: string;
  onChange: (value: string) => void;
  options: { id: string; value: string; label: string }[];
}) {
  return (
    <NativeSelect id={id} value={value} onChange={(e) => onChange(e.target.value)}>
      <option value="">Routed by policy</option>
      {options.map((o) => (
        <option key={o.id} value={o.value}>
          {o.label}
        </option>
      ))}
    </NativeSelect>
  );
}

/** Records microphone audio with MediaRecorder (when the browser supports it). */
function useRecorder(onDone: (blob: Blob) => void) {
  const recorder = useRef<MediaRecorder | null>(null);
  const [recording, setRecording] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const supported =
    typeof window !== "undefined" &&
    typeof window.MediaRecorder !== "undefined" &&
    !!navigator.mediaDevices?.getUserMedia;

  async function start() {
    setError(null);
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const chunks: Blob[] = [];
      const r = new MediaRecorder(stream);
      r.ondataavailable = (e) => chunks.push(e.data);
      r.onstop = () => {
        stream.getTracks().forEach((t) => t.stop());
        // Strip codec parameters: the API accepts the container type.
        const type = (r.mimeType || "audio/webm").split(";")[0] ?? "audio/webm";
        onDone(new Blob(chunks, { type }));
        setRecording(false);
      };
      r.start();
      recorder.current = r;
      setRecording(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : "microphone unavailable");
    }
  }

  return { supported, recording, error, start, stop: () => recorder.current?.stop() };
}

export function AudioPage() {
  const deployments = useQuery({ queryKey: ["deployments"], queryFn: () => api.deployments() });
  const toOptions = (capability: "audio_input" | "audio_output") =>
    deploymentsWith(deployments.data, capability).map((d) => ({
      id: d.id,
      value: deploymentValue(d),
      label: `${d.provider_id} / ${d.provider_model}`,
    }));
  const sttOptions = toOptions("audio_input");
  const ttsOptions = toOptions("audio_output");

  const [sttModel, setSttModel] = useState("");
  const [language, setLanguage] = useState("");
  const [clip, setClip] = useState<{ blob: Blob; name: string } | null>(null);
  const transcribe = useMutation({
    mutationFn: (input: { blob: Blob; name: string }) =>
      api.transcribe(
        input.blob,
        { ...parseDeploymentValue(sttModel), language: language || null },
        input.name,
      ),
  });
  const recorder = useRecorder((blob) => {
    const name = `recording.${blob.type.split("/")[1] ?? "webm"}`;
    setClip({ blob, name });
    transcribe.mutate({ blob, name });
  });

  const [ttsModel, setTtsModel] = useState("");
  const [text, setText] = useState("Hello from Open Model Fabric.");
  const [voice, setVoice] = useState("alloy");
  const [format, setFormat] = useState<"wav" | "mp3" | "opus" | "flac">("wav");
  const [audioUrl, setAudioUrl] = useState<string | null>(null);
  const speak = useMutation({
    mutationFn: () => api.speech({ input: text, voice, format, ...parseDeploymentValue(ttsModel) }),
    onSuccess: (result) => {
      setAudioUrl((previous) => {
        if (previous) URL.revokeObjectURL(previous);
        return URL.createObjectURL(result.audio);
      });
    },
  });
  useEffect(
    () => () => {
      if (audioUrl) URL.revokeObjectURL(audioUrl);
    },
    [audioUrl],
  );

  return (
    <div className="p-6">
      <PageHeader
        title="Audio"
        description="Speech-to-text and text-to-speech with models that declare audio_input or audio_output. Ollama has no audio API; use an OpenAI-compatible server (whisper.cpp, speaches, Kokoro-FastAPI, vLLM)."
      />
      <ErrorAlert error={deployments.error} className="mb-4" />
      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Mic className="size-4" aria-hidden /> Speech to text
            </CardTitle>
            <CardDescription>
              Upload wav, mp3, m4a, ogg, webm or flac, or record from the microphone.
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            {deployments.isSuccess && sttOptions.length === 0 && (
              <p className="text-xs text-muted-foreground">
                No available deployment declares <code>audio_input</code>.
              </p>
            )}
            <div>
              <Label htmlFor="stt-model">Model</Label>
              <ModelSelect
                id="stt-model"
                value={sttModel}
                onChange={setSttModel}
                options={sttOptions}
              />
            </div>
            <div>
              <Label htmlFor="stt-language">Language (optional, ISO-639-1)</Label>
              <Input
                id="stt-language"
                value={language}
                placeholder="en"
                onChange={(e) => setLanguage(e.target.value.trim().toLowerCase())}
              />
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <Input
                type="file"
                aria-label="Audio file"
                accept={AUDIO_ACCEPT}
                onChange={(e) => {
                  const file = e.target.files?.[0];
                  if (file) setClip({ blob: file, name: file.name });
                }}
                className="max-w-xs"
              />
              <Button
                disabled={!clip || transcribe.isPending}
                onClick={() => clip && transcribe.mutate(clip)}
              >
                Transcribe
              </Button>
              {recorder.supported &&
                (recorder.recording ? (
                  <Button variant="outline" onClick={recorder.stop}>
                    <Square /> Stop recording
                  </Button>
                ) : (
                  <Button variant="outline" onClick={() => void recorder.start()}>
                    <Mic /> Record
                  </Button>
                ))}
            </div>
            {recorder.error && <p className="text-xs text-status-bad">{recorder.error}</p>}
            <ErrorAlert error={transcribe.error} />
            {transcribe.data && (
              <div data-testid="transcription">
                <p className="whitespace-pre-wrap rounded border bg-muted/40 p-2 text-sm">
                  {transcribe.data.text}
                </p>
                <p className="mt-1 font-mono text-[11px] text-muted-foreground">
                  {transcribe.data.provider_id}/{transcribe.data.model}
                  {transcribe.data.language ? ` · ${transcribe.data.language}` : ""}
                  {transcribe.data.duration_s != null
                    ? ` · ${transcribe.data.duration_s.toFixed(2)} s audio`
                    : ""}
                  {` · ${ms(transcribe.data.timings.total_ms)}`}
                </p>
              </div>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Volume2 className="size-4" aria-hidden /> Text to speech
            </CardTitle>
            <CardDescription>
              Up to 4096 characters. The mock model produces WAV only.
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            {deployments.isSuccess && ttsOptions.length === 0 && (
              <p className="text-xs text-muted-foreground">
                No available deployment declares <code>audio_output</code>.
              </p>
            )}
            <div>
              <Label htmlFor="tts-model">Model</Label>
              <ModelSelect
                id="tts-model"
                value={ttsModel}
                onChange={setTtsModel}
                options={ttsOptions}
              />
            </div>
            <div>
              <Label htmlFor="tts-text">Text</Label>
              <Textarea
                id="tts-text"
                value={text}
                maxLength={4096}
                onChange={(e) => setText(e.target.value)}
              />
            </div>
            <div className="grid grid-cols-2 gap-2">
              <div>
                <Label htmlFor="tts-voice">Voice</Label>
                <Input id="tts-voice" value={voice} onChange={(e) => setVoice(e.target.value)} />
              </div>
              <div>
                <Label htmlFor="tts-format">Format</Label>
                <NativeSelect
                  id="tts-format"
                  value={format}
                  onChange={(e) => setFormat(e.target.value as typeof format)}
                >
                  <option value="wav">wav</option>
                  <option value="mp3">mp3</option>
                  <option value="opus">opus</option>
                  <option value="flac">flac</option>
                </NativeSelect>
              </div>
            </div>
            <div>
              <Button
                disabled={!text.trim() || !voice || speak.isPending}
                onClick={() => speak.mutate()}
              >
                Speak
              </Button>
            </div>
            <ErrorAlert error={speak.error} />
            {audioUrl && (
              <div>
                <audio controls src={audioUrl} data-testid="speech-audio" className="w-full" />
                {speak.data && (
                  <p className="mt-1 font-mono text-[11px] text-muted-foreground">
                    {speak.data.providerId}/{speak.data.model} · {speak.data.audio.type} ·{" "}
                    {speak.data.audio.size} bytes
                  </p>
                )}
              </div>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
