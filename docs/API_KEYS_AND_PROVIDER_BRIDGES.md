# API key storage and provider bridges

This guide documents Jupiter's credential and provider flow so contributors can add model backends without putting secrets in source control. It describes the public source layout and gives a safer pattern for new integrations; it is not a security audit or a guarantee that every application surface is production-ready.

## The basic design

```text
Jupiter UI / client
        |
        | prompt + app request (never a provider key)
        v
Jupiter backend
        |
        +--> choose local runtime (Ollama / llama.cpp)
        |       no hosted-provider key required
        |
        +--> route hosted request --> resolve the matching credential on the backend
                                      --> provider adapter --> provider API
```

The UI should call Jupiter's backend. The backend chooses a model/provider, resolves the credential for that request, calls the provider adapter, and returns a normalized response. A hosted provider receives the prompt and any relevant conversation or memory context included in that request. With a local runtime, requests can stay on the machine when the configured endpoint is genuinely local and does not proxy elsewhere.

## Local, single-user setup

1. Copy `.env.example` to `.env` on your own machine.
2. Add your own provider configuration locally and select a backend/model.
3. Keep `.env` out of Git. The repository `.gitignore` excludes `.env` and `.env.*`, while allowing only the blank `.env.example` template.
4. Do not put credentials in source code, frontend bundles, browser storage, URLs, screenshots, issue reports, or logs. Do not commit a populated example file.

The example file contains variable names with empty values only. A local `.env` is a plain-text convenience file: protect it with your operating-system account, do not sync it to a shared folder, and remove or rotate credentials if the machine or file is exposed. For production, use a managed secret store or an operating-system credential vault instead of a checked-in file.

## Multi-user applications

Never use one maintainer's provider key as an implicit shared key for every user. For a multi-user service, use a distinct credential scope per account and provider:

- Ask the user to supply a credential directly to the authenticated Jupiter backend over HTTPS.
- Authenticate and authorize the account before saving or using it. Derive the account identity from the server-side session; do not trust an account ID supplied by the client.
- Encrypt credentials at rest. Keep the encryption master key outside the database and source repository, ideally in a managed secret store. Restrict who and what can decrypt it.
- Resolve only the current account's credential for a request, pass it to the backend adapter in memory, and never return it to the client.
- Provide a way to remove/replace the credential. Keep logs and status responses to non-secret metadata; redact provider errors before logging.

Jupiter's account-scoped credential code is in `services/account_access.py`; request-local resolution is in `core/request_context.py`. Review these paths and the deployment's key-management setup before using the app as a multi-user service. The code pointers are implementation context, not a claim of independent security certification.

## How a provider bridge fits together

1. A caller describes a task and messages using the provider-neutral request contract in `core/provider_contracts.py` (`AIRequest`).
2. The model router selects a compatible model/provider based on task, capabilities, and privacy policy (`services/model_routing.py`).
3. The request context supplies an identity and a credential resolver (`core/request_context.py`).
4. The adapter resolves only the credential scope it needs and performs the provider-specific request (`core/provider_adapters.py`). Local backends can instead use the configured local inference endpoint.
5. Provider responses and failures are converted into Jupiter's common response/failure types so callers do not need provider-specific handling.

When adding a provider, keep its API details inside an adapter. Reuse an existing compatible adapter where possible, add the provider's credential scope and configuration name, and avoid passing keys through UI state or general conversation objects. A provider bridge must not silently change privacy routing: if a request falls back from a local model to a hosted provider, make that behavior visible and ensure the user has allowed the hosted request.

## Safe configuration example

Use names and empty values in templates; a real credential belongs only in the developer's local secret store:

```dotenv
JUPITER_LLM_BACKEND=ollama
OPENAI_API_KEY=
GEMINI_API_KEY=
```

Read a key at the backend boundary, fail closed when it is absent, and do not print it:

```python
import os

api_key = os.getenv("GEMINI_API_KEY", "").strip()
if not api_key:
    raise RuntimeError("Gemini credentials are not configured")
# Pass api_key directly to the provider SDK/client here; do not log or return it.
```

The names above are examples only. Never replace the empty values in a committed template with a real key.

## Before publishing a change

- Check that no populated `.env`, credential database, private logs, or user data is staged.
- Search the diff for accidental credentials, including provider keys, access tokens, and private URLs. Do not paste a discovered secret into a report; revoke/rotate it and remove it from current files and Git history.
- Confirm the adapter sends the key only to the intended provider over HTTPS and does not include it in errors, telemetry, or response bodies.
- Describe which request data leaves the device for hosted inference. Do not describe hosted inference as fully local or private from the provider.

## Source pointers

- `.env.example` and `.gitignore` — local configuration template and ignore rules.
- `core/provider_contracts.py` — provider-neutral request, response, and credential-scope types.
- `core/request_context.py` — request-local identity and credential resolution.
- `services/model_routing.py` and `services/credential_router.py` — model and credential selection.
- `core/provider_adapters.py` — provider request adapters.
- `services/account_access.py` — account-scoped credential storage paths.
