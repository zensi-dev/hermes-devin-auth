# hermes-devin-auth

Use [Devin](https://app.devin.ai)'s SWE models in
[Hermes Agent](https://github.com/NousResearch/hermes-agent) — browser sign-in
plus a native model provider. No Devin CLI, no ACP subprocess, no `devin` tool
calls.

## What you get

- **`hermes devin login`** — browser OAuth (PKCE S256) against `app.devin.ai`,
  local callback on `127.0.0.1:59653`, token exchange at
  `api.devin.ai/auth/cli/token`. The session JWT is stored in Hermes'
  credential pool (`auth.json`).
- **`--provider devin`** — a `devin` model provider that talks directly to the
  Devin Cascade API (`server.codeium.com`) over Connect-protocol protobuf:
  `AuthService/GetUserJwt` → `ApiServerService/GetChatMessage` (gzip Connect
  frames) → OpenAI-shaped streaming chunks.
- **`hermes devin status` / `hermes devin models` / `/devin`** — sign-in state,
  live model list via `GetCliModelConfigs`, in-session status.

## Install

One command — installs and enables both plugins, pinned to an exact commit:

```bash
hermes plugins pack install https://raw.githubusercontent.com/zensi-dev/hermes-devin-auth/main/hermes-pack.yaml
```

Then sign in and use it:

```bash
hermes devin login                              # browser OAuth
hermes -z "fix the tests" --provider devin -m swe-1-6
# or inside a session: /model devin
```

### Alternative: install each plugin separately

```bash
hermes plugins install zensi-dev/hermes-devin-auth#plugins/model-providers/devin
hermes plugins install zensi-dev/hermes-devin-auth#plugins/devin
hermes plugins enable devin
```

Each half installs flat into `~/.hermes/plugins/`; the provider's
`kind: model-provider` manifest routes it to provider discovery automatically.

### Requirements

- Hermes Agent with plugin support
- `httpx` (`pip install httpx` — Hermes prints this hint at install; it never
  auto-installs plugin dependencies)
- A Devin account

## Layout

```
hermes-pack.yaml               # one-command install manifest (pack install)
plugins/
├── model-providers/devin/     # provider plugin (auto-loaded, no enable needed)
│   ├── plugin.yaml
│   ├── __init__.py            # ProviderProfile + register_provider()
│   ├── client.py              # Connect/protobuf client (chat.completions.create)
│   ├── oauth.py               # PKCE login + credential-pool persistence
│   └── proto_wire.py          # minimal protobuf codec (no protobuf dep)
└── devin/                     # CLI/slash-command plugin (needs plugins.enabled)
    ├── plugin.yaml
    └── __init__.py            # hermes devin <cmd> + /devin
```

Two plugins because Hermes loads them differently: model-provider plugins are
imported for their `register_provider()` side effect (no `ctx`), while CLI
commands need `register(ctx)`. The CLI plugin loads the provider package by
path so both share one implementation. The provider manifest is named
`devin-provider` so the two halves don't collide when installed flat into
`~/.hermes/plugins/`; the registered provider name stays `devin`.

## Auth notes

- `DEVIN_API_KEY` env var overrides the stored credential (standard api_key
  precedence).
- `hermes auth add devin` also works for a manually-pasted token.
- `hermes devin logout` removes the OAuth credential from the pool.

## Wire notes

- Session token is prefixed `devin-session-token$` before use.
- `GetUserJwt` returns a short-lived user JWT + optional custom API server URL;
  cached until the JWT's own expiry, refreshed on 401.
- `GetChatMessage` request: `metadata` (windsurf IDE identity), `prompt`
  (system), `chat_message_prompts` (user/system/tool), `chat_model_uid`,
  `request_type=CASCADE`, `CompletionConfiguration` (max_tokens, temperature,
  top_p, stop patterns), `tools`, `tool_choice{auto}`, `cascade_id` (threaded
  per Hermes session via `extra_body.devin_cascade_id`).
- Response stream: Connect frames (flag + 4-byte length + gzip payload);
  `delta_text`/`delta_thinking`/`delta_tool_calls`/`stop_reason`/`usage` map to
  OpenAI `ChatCompletionChunk` deltas. Cumulative `arguments_json` snapshots are
  diffed to deltas.
- `stop_reason` → `finish_reason`: `FUNCTION_CALL`/any tool call → `tool_calls`,
  `MAX_TOKENS` → `length`, else `stop`.
