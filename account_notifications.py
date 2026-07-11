"""Best-effort account status emails (failures do not block admin actions)."""

import os

from email_service import (
    is_email_configured,
    merge_email_config,
    send_account_approved_email,
    send_account_rejected_email,
)


def _resolve_data_dir():
    from user_auth import get_auth_data_dir
    return get_auth_data_dir()


def _app_login_url():
    base = (os.environ.get('APP_BASE_URL') or '').strip().rstrip('/')
    if base:
        return f'{base}/login'
    return '/login'


def _app_subscribe_url():
    base = (os.environ.get('APP_BASE_URL') or '').strip().rstrip('/')
    if base:
        return f'{base}/subscribe'
    return '/subscribe'


def _recipient_for_user(user):
    if not user:
        return None
    email = (user.get('email') or '').strip()
    return email or None


def _deliver(user, send_callable):
    try:
        to_email = _recipient_for_user(user)
        if not to_email:
            return
        config = merge_email_config(data_dir=_resolve_data_dir())
        if not is_email_configured(config):
            return
        send_callable(config, to_email)
    except Exception:
        return


def notify_account_approved(user):
    login_url = _app_login_url()
    subscribe_url = _app_subscribe_url()

    def _send(config, to_email):
        send_account_approved_email(
            config,
            to_email,
            login_url=login_url,
            subscribe_url=subscribe_url,
        )

    _deliver(user, _send)


def notify_account_rejected(user):
    login_url = _app_login_url()

    def _send(config, to_email):
        send_account_rejected_email(config, to_email, login_url=login_url)

    _deliver(user, _send)
