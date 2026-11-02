# Jupiter

Jupiter is a local-first personal AI companion. It combines conversational planning and user-scoped memory with configurable model backends, so people can run it on their own computer and choose a local model or configure a hosted provider.

> **Privacy depends on the model you choose.** Jupiter stores application memory locally by default. With Ollama or a local `llama.cpp` server, model requests can stay on your machine. If you choose a hosted provider, Jupiter sends the prompt and relevant conversation or memory context needed for that request to that provider. Review the provider’s privacy and retention terms.

## Included

- Conversation planning, language adaptation, response checks, and memory components.
- User-scoped local memory storage and retrieval.
- Adapters for Ollama, `llama.cpp`, OpenAI-compatible APIs, and Gemini.
- Backend, desktop, and mobile source code.
- A separate visual showcase is available in [Jupiter-Website](https://github.com/Kingarup12/jupiter-website) when both projects are checked out together.

Jupiter is under active development. Some app surfaces and integrations may require additional setup and may not be production-ready. The website showcase is not itself a chat service.

## Quick start

Requirements: Python 3.11 or newer, Git, and either a local model runtime or an API account for a supported hosted provider.

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` on your own machine. Never commit it. Choose a backend and configure the model/provider settings and, if using a hosted provider, your own API key. API usage may incur charges under that provider’s terms.

Start the terminal companion with:

```powershell
python -m app.main
```

The server entry point is `app.server`. Review its security and configuration requirements before exposing it beyond your own machine.

## Data and provider requests

By default, Jupiter stores user-scoped memory and application data under `data/`; logs are written under `logs/`. These directories are ignored by Git. Do not add real conversations, memory databases, logs, credentials, or screenshots containing personal information to issues or pull requests.

A local model runtime can keep inference on-device, subject to its configuration. A remote API receives request content. Review provider settings before using private or sensitive information. The website showcase is a separate frontend and does not connect to this backend.

## Contributing

Issues and pull requests are welcome. Do not include personal data, API keys, access tokens, private logs, or live user databases in reports. See [SECURITY.md](.github/SECURITY.md) for security reporting guidance.

## License

No license has been selected for this public source export. Until the author adds one, ordinary copyright applies and reuse is not automatically permitted.
