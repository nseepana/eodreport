"""Resolve and validate Kite access tokens for the EOD report cron job."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from kiteconnect import KiteConnect
from kiteconnect.exceptions import TokenException

from eod_report.config import EodReportConfig

log = logging.getLogger(__name__)

LOGIN_HINT = (
    "Kite login required — log in via kiteob (token syncs to MongoDB kite_webhook_access) "
    "or set KITE_ACCESS_TOKEN in .env"
)


@dataclass(frozen=True)
class KiteAuth:
    token: str | None
    source: str  # env | mongodb | none
    valid: bool
    user_id: str | None = None
    user_name: str | None = None
    login_required: bool = False
    message: str | None = None


def _token_candidates(cfg: EodReportConfig) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    env_tok = (cfg.kite_access_token or "").strip()
    if env_tok:
        out.append(("env", env_tok))
        seen.add(env_tok)
    try:
        from kite_session_store import fetch_access_token

        mongo_tok = fetch_access_token()
        if mongo_tok and mongo_tok not in seen:
            out.append(("mongodb", mongo_tok))
    except Exception as exc:  # noqa: BLE001
        log.debug("mongo token lookup failed: %s", exc)
    return out


def _validate_token(api_key: str, token: str) -> tuple[bool, dict[str, Any] | None, str | None]:
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(token)
    try:
        profile = kite.profile()
        return True, profile, None
    except TokenException as exc:
        return False, None, str(exc)
    except Exception as exc:  # noqa: BLE001
        return False, None, str(exc)


def resolve_kite_auth(cfg: EodReportConfig) -> KiteAuth:
    """Pick a working token from .env then Mongo; probe profile() to confirm session."""
    api_key = (cfg.kite_api_key or "").strip()
    if not api_key:
        return KiteAuth(
            token=None,
            source="none",
            valid=False,
            login_required=True,
            message="KITE_API_KEY is not configured in .env",
        )

    candidates = _token_candidates(cfg)
    if not candidates:
        return KiteAuth(
            token=None,
            source="none",
            valid=False,
            login_required=True,
            message=f"No Kite access token in .env or MongoDB.\n{LOGIN_HINT}",
        )

    errors: list[str] = []
    for source, token in candidates:
        ok, profile, err = _validate_token(api_key, token)
        if ok and profile:
            return KiteAuth(
                token=token,
                source=source,
                valid=True,
                user_id=str(profile.get("user_id") or "") or None,
                user_name=str(profile.get("user_name") or "") or None,
            )
        errors.append(f"{source}: {err or 'invalid'}")

    return KiteAuth(
        token=None,
        source="none",
        valid=False,
        login_required=True,
        message="Kite token expired or invalid (" + "; ".join(errors) + f").\n{LOGIN_HINT}",
    )


def ensure_kite_for_live(auth: KiteAuth, *, live_session: bool) -> str | None:
    """Return token when OK; log and return None when optional; raise via caller on required miss."""
    if auth.valid and auth.token:
        label = auth.user_name or auth.user_id or "ok"
        log.info("kite connected (%s) token_source=%s", label, auth.source)
        return auth.token
    if live_session:
        return None
    log.warning(
        "no valid kite token — continuing with NSE/Yahoo only%s",
        f" ({auth.message})" if auth.message else "",
    )
    return None
