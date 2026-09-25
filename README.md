# Hermes Devin Auth

Use [Devin](https://app.devin.ai)'s SWE coding models in
[Hermes Agent](https://github.com/NousResearch/hermes-agent). Sign in with your
Devin account, choose a model, and start working in Hermes.

You get browser sign-in, a picker for available models, streaming responses,
and support for Hermes tool calling. You do not need to install the Devin CLI.

## Before you start

- Install Hermes Agent with support for `hermes plugins install` and
  model-provider plugins.
- Make sure Git is available in your terminal: `git --version`.
- Have a Devin account and a browser available for sign-in.

## Get started

### 1. Install both plugins

Run both commands in your terminal, using the full paths shown:

```bash
hermes plugins install zensi-dev/hermes-devin-auth/plugins/model-providers/devin --enable
hermes plugins install zensi-dev/hermes-devin-auth/plugins/devin --enable
```

Both are required: `devin-provider` connects Hermes to Devin's models, and
`devin` adds the sign-in and model-selection commands. Accept any dependency
prompts during installation.

If you use multiple Hermes profiles, install and run the plugins in the same
profile. Restart any existing Hermes sessions after installation.

### 2. Sign in and choose a model

```bash
hermes devin login
```

Complete sign-in in your browser, then return to the terminal. In an
interactive terminal, the model picker opens automatically. Your selection
becomes the default model for Hermes, using Devin as the provider.

If the picker does not open, run `hermes devin use` to choose your model.

Check that sign-in and API access are working:

```bash
hermes devin status
```

If the browser does not open, follow the
[manual sign-in steps](#the-browser-does-not-open-or-hermes-runs-on-another-machine).

### 3. Start using Hermes

Start a session with your selected default model:

```bash
hermes
```

Or give Hermes a task directly:

```bash
hermes -z "fix the tests"
```

Inside an existing Hermes session, use `/model devin` to select a Devin model.

## Everyday commands

Run these in your terminal:

| Command | What it does |
| --- | --- |
| `hermes devin login` | Sign in through your browser and choose a default model. |
| `hermes devin use` | Choose a different default model from the available list. |
| `hermes devin models` | List available model IDs. |
| `hermes devin status` | Check sign-in, session expiry, and API access. |
| `hermes devin status --no-check` | Check sign-in and session expiry without contacting the API. |
| `hermes devin logout` | Remove the saved browser sign-in credentials. |

Inside a Hermes session, use `/devin` to check sign-in status or `/model devin`
to switch models.

## Sign out or change accounts

To remove your saved browser sign-in:

```bash
hermes devin logout
```

To use another account, sign out, then run `hermes devin login` and sign in
with that account in your browser.

If you previously set `DEVIN_API_KEY`, it takes priority over your saved
browser sign-in. Logout does not remove that environment variable or
credentials added with `hermes auth add devin`; manage those separately if
you use them.

## Alternative installation: pinned pack

If `hermes plugins pack --help` is available, you can install both plugins
with one command at the exact versions recorded in the pack:

```bash
hermes plugins pack install https://raw.githubusercontent.com/zensi-dev/hermes-devin-auth/main/hermes-pack.yaml
```

Run this in an interactive terminal and confirm the pack review and any
per-plugin prompts. Successful entries are enabled. Check that both plugins
installed successfully, then continue with
[sign-in and model selection](#2-sign-in-and-choose-a-model).

If `pack` is unrecognized, use the [two installation commands](#1-install-both-plugins)
above. The pack's recorded versions may differ from the latest repository
version.

## Troubleshooting

### Installation fails with a repository or Git error

Use the full slash-separated paths in the installation commands and check
that `git --version` works.

### A plugin already exists

Check what is installed:

```bash
hermes plugins list
```

If both `devin-provider` and `devin` are present, enable them:

```bash
hermes plugins enable devin-provider
hermes plugins enable devin
```

To replace an existing plugin, back up any edits you made inside its plugin
directory, then repeat its installation command with `--force`. Plugin
directories are normally under `~/.hermes/plugins/`.

### `hermes devin` is missing or reports a missing provider

Check that both `devin` and `devin-provider` are installed and enabled in your
active Hermes profile. If only one installed successfully, fix the failed
installation before signing in. Restart existing Hermes sessions afterward.

### Installation reports a missing Python dependency

The provider requires `httpx>=0.24,<1`. Recent Hermes versions manage this
dependency during installation. If your version only prints a dependency
hint, update Hermes or install the dependency into **Hermes' own Python
environment**, not an unrelated system Python.

### The browser does not open or Hermes runs on another machine

Run `hermes devin login` in a terminal. Open the sign-in URL printed there in
your browser and complete sign-in. Copy the full callback URL you are
redirected to and paste it into the waiting terminal.

### Your session has expired or API access fails

Run `hermes devin status` to see the error. If the session has expired, run
`hermes devin login` again. If you have set `DEVIN_API_KEY`, check it too:
it overrides the saved browser sign-in.

### A pack-installed plugin will not update

Pack-installed plugins are pinned; ordinary `hermes plugins update` does not
move their pins. To reinstall both at the pack's recorded versions, back up
any edits inside their plugin directories and repeat the pack installation
command with `--force`. This uses the versions recorded in the pack, which
may differ from the latest repository version.

### Still stuck?

When reporting a problem, include the command you ran, the error message,
and the output of `hermes --version`. Remove any tokens or sign-in callback
URLs before sharing.
