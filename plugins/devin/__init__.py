"""Devin plugin — ``hermes devin login|logout|status|models`` + ``/devin``.

The companion model-provider plugin (``plugins/model-providers/devin``)
carries the Cascade client; this plugin owns the OAuth login UX and writes
the session token into Hermes' credential pool where the provider's standard
api_key resolution finds it.

The provider package is loaded by path (``../model-providers/devin`` or the
flat-install sibling ``../devin-provider``) so both plugins share one
implementation regardless of import order.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_PLUGIN_DIR = Path(__file__).resolve().parent
_PROVIDER_PKG_DIRS = (
    # manual-copy layout: plugins/model-providers/devin/
    _PLUGIN_DIR.parent / "model-providers" / "devin",
    # `hermes plugins install <repo>#plugins/model-providers/devin` — flat sibling
    _PLUGIN_DIR.parent / "devin-provider",
)
_PKG_NAME = "_hermes_devin_pkg"
# Module names providers/__init__.py uses when it imports the provider plugin
# itself — reuse that instance instead of double-importing (double registration
# is harmless but wasteful).
_DISCOVERY_MODULE_NAMES = (
    "_hermes_user_provider_devin_provider",  # flat install: dir devin-provider
    "_hermes_user_provider_devin",           # model-providers/devin
    "plugins.model_providers.devin",         # bundled
)


def _provider_pkg_dir():
    for candidate in _PROVIDER_PKG_DIRS:
        if (candidate / "__init__.py").exists():
            return candidate
    return None


def _load_provider_pkg():
    """Import the sibling model-provider package by path; None if absent."""
    existing = sys.modules.get(_PKG_NAME)
    if existing is not None:
        return existing
    for name in _DISCOVERY_MODULE_NAMES:
        loaded = sys.modules.get(name)
        if loaded is not None:
            sys.modules[_PKG_NAME] = loaded
            return loaded
    pkg_dir = _provider_pkg_dir()
    if pkg_dir is None:
        return None
    try:
        spec = importlib.util.spec_from_file_location(
            _PKG_NAME, pkg_dir / "__init__.py",
            submodule_search_locations=[str(pkg_dir)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[_PKG_NAME] = module
        spec.loader.exec_module(module)
        return module
    except Exception as exc:
        logger.warning("devin: failed to load provider package: %s", exc)
        sys.modules.pop(_PKG_NAME, None)
        return None


def _oauth():
    pkg = _load_provider_pkg()
    if pkg is None:
        return None
    try:
        import importlib
        return importlib.import_module(f"{_PKG_NAME}.oauth")
    except Exception as exc:
        logger.warning("devin: failed to load oauth module: %s", exc)
        return None


def _client_mod():
    pkg = _load_provider_pkg()
    if pkg is None:
        return None
    try:
        import importlib
        return importlib.import_module(f"{_PKG_NAME}.client")
    except Exception as exc:
        logger.warning("devin: failed to load client module: %s", exc)
        return None


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def _cmd_login(args) -> int:
    oauth = _oauth()
    if oauth is None:
        print("devin: provider package not found — install plugins/model-providers/devin")
        return 1
    print("Opening Devin sign-in in your browser…")
    print("If it doesn't open (or Hermes runs on another machine), copy the URL below,")
    print("sign in, then paste the callback URL you're redirected to here.")
    try:
        token = oauth.run_login_flow(
            on_url=lambda u: print(f"\n{u}\n\nPaste callback URL (or press Enter to keep waiting): ", end="", flush=True))
    except oauth.DevinLoginError as exc:
        print(f"devin: login failed — {exc}")
        return 1
    except KeyboardInterrupt:
        print("\ndevin: login cancelled")
        return 130
    oauth.save_session_token(token)
    label = oauth.token_subject(token)
    print(f"devin: signed in{f' as {label}' if label else ''}.\n")
    # Chain straight into model selection when interactive — the whole point of
    # signing in is picking a model.
    if sys.stdin.isatty():
        return _pick_and_save_model(token)
    print("Run `hermes devin use` to pick a model.")
    return 0


def _cmd_logout(args) -> int:
    oauth = _oauth()
    if oauth is None:
        print("devin: provider package not found")
        return 1
    removed = oauth.clear_oauth_credentials()
    print(f"devin: removed {removed} OAuth credential(s)."
          " (DEVIN_API_KEY env var and `hermes auth add devin` keys are untouched.)")
    return 0


def _cmd_status(args) -> int:
    oauth = _oauth()
    if oauth is None:
        print("devin: provider package not found")
        return 1
    token = oauth.load_session_token()
    if not token:
        print("devin: not signed in — run `hermes devin login` or set DEVIN_API_KEY")
        return 1
    import time
    exp_ms = oauth.token_expiry_ms(token)
    remaining = (exp_ms - time.time() * 1000) / 1000
    label = oauth.token_subject(token)
    print(f"devin: signed in{f' as {label}' if label else ''}")
    if remaining <= 0:
        print("devin: session token expired — run `hermes devin login`")
        return 1
    print(f"devin: token expires in {remaining / 3600:.1f}h")
    if not getattr(args, "no_check", False):
        client = _client_mod()
        if client is not None:
            try:
                models = client.list_models(token, timeout=8.0)
                print(f"devin: API reachable — {len(models)} model(s): {', '.join(models[:8])}"
                      + ("…" if len(models) > 8 else ""))
            except Exception as exc:
                print(f"devin: API check failed — {exc}")
                return 1
    return 0


def _pick_model(models, current: str = ""):
    """Arrow-key picker over the live model list; falls back to numbered input.

    List-only by design — no free-text model entry. Returns the chosen model
    id, or None on cancel.
    """
    ordered = list(dict.fromkeys(
        ([current] if current and current in models else []) + list(models)))
    try:
        from hermes_cli.curses_ui import curses_radiolist
        idx = curses_radiolist(
            "Select default model:", ordered,
            selected=0, cancel_returns=-1, searchable=True)
        return ordered[idx] if 0 <= idx < len(ordered) else None
    except Exception:
        pass
    for i, m in enumerate(ordered, 1):
        marker = " (current)" if m == current else ""
        print(f"  {i}. {m}{marker}")
    try:
        raw = input("Model number (Enter to cancel): ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not raw:
        return None
    try:
        n = int(raw)
    except ValueError:
        return None
    return ordered[n - 1] if 1 <= n <= len(ordered) else None


def _save_devin_model(selected: str) -> None:
    """Persist model.default + model.provider=devin; the provider profile owns
    the endpoint, so stale base_url/api_key/api_mode keys are dropped."""
    from hermes_cli.config import load_config, save_config
    cfg = load_config()
    model = cfg.get("model")
    if not isinstance(model, dict):
        model = {"default": model} if model else {}
        cfg["model"] = model
    model["default"] = selected
    model["provider"] = "devin"
    for stale in ("base_url", "api_key", "api_mode"):
        model.pop(stale, None)
    save_config(cfg)
    try:
        from hermes_cli.auth import deactivate_provider
        deactivate_provider()
    except Exception:
        pass


def _pick_and_save_model(token: str) -> int:
    """Fetch live models → picker → persist. Shared by `use` and post-login."""
    client = _client_mod()
    if client is None:
        print("devin: provider package not found")
        return 1
    try:
        models = client.list_models(token, timeout=10.0)
    except Exception as exc:
        print(f"devin: model list failed — {exc}")
        return 1
    if not models:
        print("devin: no models returned")
        return 1
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        model_cfg = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}
        current = model_cfg.get("default", "") if model_cfg.get("provider") == "devin" else ""
    except Exception:
        current = ""
    selected = _pick_model(models, current)
    if not selected:
        print("No change.")
        return 0
    _save_devin_model(selected)
    print(f"Default model set to: {selected} (via Devin)")
    return 0


def _cmd_use(args) -> int:
    oauth = _oauth()
    if oauth is None:
        print("devin: provider package not found")
        return 1
    token = oauth.load_session_token()
    if not token:
        try:
            answer = input("devin: not signed in. Run browser sign-in now? [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return 1
        if answer in ("n", "no"):
            return 1
        try:
            token = oauth.run_login_flow(
                on_url=lambda u: print(f"\n{u}\n\nPaste callback URL (or press Enter to keep waiting): ", end="", flush=True))
            oauth.save_session_token(token)
            print("devin: signed in.\n")
        except Exception as exc:
            print(f"devin: login failed — {exc}")
            return 1
    return _pick_and_save_model(token)


def _cmd_models(args) -> int:
    oauth = _oauth()
    client = _client_mod()
    if oauth is None or client is None:
        print("devin: provider package not found")
        return 1
    token = oauth.load_session_token()
    if not token:
        print("devin: not signed in — run `hermes devin login`")
        return 1
    try:
        models = client.list_models(token, timeout=10.0)
    except Exception as exc:
        print(f"devin: model list failed — {exc}")
        return 1
    if not models:
        print("devin: no models returned")
        return 0
    for m in models:
        print(m)
    return 0

def _cmd_help(args) -> int:
    print("devin — Devin provider auth\n"
          "\n"
          "  hermes devin login     Sign in via browser (OAuth PKCE)\n"
          "  hermes devin logout    Remove the OAuth credential\n"
          "  hermes devin status    Show sign-in state + API check\n"
          "  hermes devin models    List available models\n"
          "  hermes devin use       Pick a model and set it as default\n"
          "\n"
          "Then use `--provider devin` or `/model devin` inside a session.")
    return 0


def _setup_devin(parser):
    """argparse wiring for ``hermes devin <subcommand>``.

    ``setup_fn`` receives the plugin's own parser (already added as
    ``hermes devin``); we attach sub-subparsers to it.
    """
    sub = parser.add_subparsers(dest="devin_command")

    p = sub.add_parser("login", help="Sign in to Devin via browser OAuth")
    p.set_defaults(func=_cmd_login)

    p = sub.add_parser("logout", help="Remove the Devin OAuth credential")
    p.set_defaults(func=_cmd_logout)

    p = sub.add_parser("status", help="Show Devin sign-in state")
    p.add_argument("--no-check", action="store_true", help="Skip the live API check")
    p.set_defaults(func=_cmd_status)

    p = sub.add_parser("models", help="List available Devin models")
    p.set_defaults(func=_cmd_models)

    p = sub.add_parser("use", help="Pick a Devin model and set it as default")
    p.set_defaults(func=_cmd_use)

    parser.set_defaults(func=_cmd_help)


# ---------------------------------------------------------------------------
# Slash command
# ---------------------------------------------------------------------------

def _slash_devin(raw_args: str):
    """``/devin`` — status + login kickoff inside a session."""
    oauth = _oauth()
    if oauth is None:
        return "devin: provider package not found"
    arg = (raw_args or "").strip().lower()
    if arg == "login":
        # Run the blocking browser flow off the chat loop; the token lands in
        # the pool when the user finishes in the browser.
        def _run():
            try:
                token = oauth.run_login_flow()
                oauth.save_session_token(token)
            except Exception as exc:
                logger.warning("devin: background login failed: %s", exc)
        threading.Thread(target=_run, daemon=True).start()
        return ("devin: opening Devin sign-in in your browser — complete it there; "
                "the credential is saved automatically.")
    token = oauth.load_session_token()
    if not token:
        return ("devin: not signed in. Run `/devin login` here or "
                "`hermes devin login` in a terminal.")
    import time
    remaining = (oauth.token_expiry_ms(token) - time.time() * 1000) / 1000
    if remaining <= 0:
        return "devin: session expired — run `/devin login` or `hermes devin login`"
    label = oauth.token_subject(token)
    return (f"devin: signed in{f' as {label}' if label else ''} "
            f"({remaining / 3600:.1f}h left). `/model devin` to switch models.")


def register(ctx):
    ctx.register_cli_command(
        "devin",
        help="Devin provider auth (login/logout/status/models)",
        setup_fn=_setup_devin,
        handler_fn=None,
        description="Devin OAuth login + status for the devin model provider",
    )
    ctx.register_command(
        "devin",
        _slash_devin,
        description="Devin sign-in status (`/devin login` to authenticate)",
        args_hint="[login]",
    )
