# Model licensing

> This page is general information, not legal advice. Always read the license and model
> card of the exact model revision you deploy.

## Project code vs model weights

- Open Model Fabric's source code is licensed under **Apache-2.0**.
- Model weights are **separate works** with their own licenses. OMF does not
  redistribute weights, and the repository must **never** contain them. `.gitignore`
  excludes `*.safetensors`, `*.gguf`, `*.bin`, `*.pt`, `*.pth`, `*.onnx`, `models/` and
  `artifacts/`. Weights are downloaded by the runtime you operate (Ollama, vLLM,
  transformers in the Colab notebook) or stored in your own artifact storage.

## "Open weights" is not "open source"

Many models publish weights under licenses that are not OSI-approved open-source
licenses. Common restrictions:

- acceptable-use policies that prohibit some uses;
- user thresholds (for example, above N monthly active users a separate license is
  required);
- research-only or non-commercial terms;
- naming or attribution requirements for derivatives;
- restrictions on using outputs to train other models.

OMF records license metadata in `ModelDefinition.license` (`name`, `spdx_id`, `url`,
`commercial_use: allowed | restricted | unknown`, `notes`), and the Model Registry screen
shows it. `commercial_use` is a summary for orientation. It is not a determination.

## Catalog summary (`configs/models.yaml`)

| Model | License (as recorded) | SPDX | Commercial use (summary) | Notes |
|---|---|---|---|---|
| Qwen2.5 0.5B / 1.5B / 7B Instruct | Apache License 2.0 | Apache-2.0 | allowed | Some Qwen2.5 sizes (e.g. 3B, 72B) use different Qwen licenses; check each size |
| Qwen2.5-VL 3B Instruct | Qwen Research License | — | restricted | Research license for the 3B variant |
| Llama 3.2 1B Instruct | Llama 3.2 Community License | — | restricted | Custom license with acceptable-use policy and a 700M MAU threshold; not OSI open source |
| Nomic Embed Text v1.5 | Apache License 2.0 | Apache-2.0 | allowed | |

When you add a model to the catalog, fill in `license`. If you are unsure, set
`commercial_use: unknown` instead of guessing.
