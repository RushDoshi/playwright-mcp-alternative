"""UI-test environment configuration (Layer A).

Environment *data* (URLs, timeouts, per-feature navigation) lives in
``config/environments.yaml`` — committed, non-secret. Every credential and token
still comes from :mod:`quality_os.config` (the host ``.env``), so this module adds
no new secret surface.

Selection: ``TEST_ENV`` in ``.env`` picks which block to use. Read from the
``Settings`` object (never straight from ``os.environ``) — ``pydantic-settings``
parses ``.env`` into the model without touching the process environment, so
reading ``os.environ`` directly here would silently see nothing.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

# repo root = .../src/quality_os/uitest/envs.py -> parents[3]
ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = ROOT / "config" / "environments.yaml"


class EnvConfigError(RuntimeError):
    """environments.yaml missing, malformed, or TEST_ENV not present in it."""


@lru_cache
def _raw(path: str | None = None) -> dict:
    p = Path(path) if path else DEFAULT_CONFIG
    if not p.exists():
        raise EnvConfigError(f"environments.yaml not found at {p}")
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:  # noqa: BLE001
        raise EnvConfigError(f"environments.yaml is not valid YAML: {e}") from e


def env_names(path: str | None = None) -> list[str]:
    return sorted((_raw(path).get("environments") or {}).keys())


def load_ui_config(settings, ticket: str | None = None,
                   env_name: str | None = None, path: str | None = None) -> dict:
    """Merge YAML environment data with host secrets into one flat dict.

    ``settings`` is a :class:`quality_os.config.Settings`; secrets are read from it
    so credentials never appear in YAML.
    """
    data = _raw(path)
    envs = data.get("environments") or {}
    name = env_name or settings.test_env or data.get("default_env")
    if name not in envs:
        raise EnvConfigError(
            f"TEST_ENV='{name}' not found in environments.yaml "
            f"(available: {', '.join(sorted(envs)) or 'none'})")
    env = envs[name] or {}
    if not env.get("login_url"):
        raise EnvConfigError(f"environment '{name}' is missing 'login_url'")

    return {
        "env_name": name,
        "login_url": env["login_url"],
        "home_match": env.get("home_match", "/home|dashboard"),
        "nav_timeout_ms": int(env.get("nav_timeout_ms", 45000)),
        "field_timeout_ms": int(env.get("field_timeout_ms", 20000)),
        "features": env.get("features") or {},
        # {continue_text, password_label, submit_text} -- see generator.py's
        # render_spec(), which uses this to deterministically rewrite a
        # fill/password or click/submit DSL step into its generic
        # (fill_named/click_text) equivalent whenever that selector wasn't
        # actually discovered (a progressive login). Empty for an environment
        # with no such config -- render_spec() then renders the DSL as given.
        "progressive_login": env.get("progressive_login") or {},
        # secrets from the host .env (never from YAML)
        "test_user": settings.test_user,
        "test_pass": settings.test_pass,
        "jira_base": settings.jira_base_url.rstrip("/"),
        "jira_email": settings.jira_email,
        "jira_token": settings.jira_api_token,
        "ticket": ticket or settings.ticket_key,
    }
