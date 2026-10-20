import { expect, test } from "@playwright/test";

/** A valid 16-bit mono WAV (0.25 s of a 440 Hz tone) built in memory. */
function wav(seconds = 0.25, rate = 16000): Buffer {
  const frames = Math.floor(seconds * rate);
  const data = Buffer.alloc(frames * 2);
  for (let i = 0; i < frames; i++) {
    data.writeInt16LE(Math.round(8000 * Math.sin((2 * Math.PI * 440 * i) / rate)), i * 2);
  }
  const header = Buffer.alloc(44);
  header.write("RIFF", 0);
  header.writeUInt32LE(36 + data.length, 4);
  header.write("WAVE", 8);
  header.write("fmt ", 12);
  header.writeUInt32LE(16, 16);
  header.writeUInt16LE(1, 20); // PCM
  header.writeUInt16LE(1, 22); // mono
  header.writeUInt32LE(rate, 24);
  header.writeUInt32LE(rate * 2, 28);
  header.writeUInt16LE(2, 32);
  header.writeUInt16LE(16, 34);
  header.write("data", 36);
  header.writeUInt32LE(data.length, 40);
  return Buffer.concat([header, data]);
}

test("embeddings page compares texts with a similarity matrix", async ({ page }) => {
  await page.goto("/embeddings");
  await page.getByLabel("Model").selectOption({ label: "mock / mock-embed" });
  await page.getByRole("button", { name: /Embed 3 texts/ }).click();
  await expect(page.getByTestId("embedding-meta")).toContainText("mock/mock-embed · 64 dimensions");
  const matrix = page.getByTestId("similarity-matrix");
  await expect(matrix.locator("tbody tr")).toHaveCount(3);
  await expect(matrix.locator("tbody tr").first().locator("td").first()).toHaveText("1.000");
});

test("audio page transcribes an uploaded WAV and plays synthesized speech", async ({ page }) => {
  await page.goto("/audio");
  const clip = wav();
  await page.getByLabel("Audio file").setInputFiles({
    name: "tone.wav",
    mimeType: "audio/wav",
    buffer: clip,
  });
  await page.getByRole("button", { name: "Transcribe" }).click();
  const transcription = page.getByTestId("transcription");
  await expect(transcription).toContainText(`mock transcription of ${clip.length} bytes`);
  await expect(transcription).toContainText("mock/mock-whisper · 0.25 s audio");

  await page.getByLabel("Text", { exact: true }).fill("Testing speech output");
  await page.getByRole("button", { name: "Speak" }).click();
  const audio = page.getByTestId("speech-audio");
  await expect(audio).toHaveAttribute("src", /^blob:/);
  // The browser can decode what the API returned: duration becomes known.
  await expect
    .poll(() => audio.evaluate((el: HTMLAudioElement) => el.duration), { timeout: 10_000 })
    .toBeGreaterThan(0);
  await expect(page.getByText(/mock\/mock-tts · audio\/wav/)).toBeVisible();
});
