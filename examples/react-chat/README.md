# React chat example

A minimal chat UI (~100 lines) built only on `@omf/sdk`. It streams from whichever
provider you pick — the mock, Ollama or any OpenAI-compatible server — with no code
changes. Start the API (`make dev-api` from the repository root), then:

```bash
pnpm --filter @omf/example-react-chat dev   # http://localhost:5174
```
