"""Best-effort billing confirmation emails (failures do not block purchases)."""

import os
from datetime import date, datetime, timezone

from email_service import (
    is_email_configured,
    merge_email_config,
    send_monthly_subscription_activation_email,
    send_one_day_pass_activation_email,
    send_promo_access_activation_email,
    send_subscription_cancellation_email,
)


def _manage_subscription_url():
    base = (os.environ.get('APP_BASE_URL') or '').strip().rstrip('/')
    if base:
        return f'{base}/manage-subscription'
    return '/manage-subscription'


def _format_date(value):
    if value is None:
        return '—'
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        return value.strftime('%B %d, %Y')
    else:
        text = str(value).strip()
        if not text:
            return '—'
        if text.endswith('Z'):
            text = text[:-1] + '+00:00'
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            try:
                return date.fromisoformat(text[:10]).strftime('%B %d, %Y')
            except ValueError:
                return text
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime('%B %d, %Y')


def _format_datetime(value):
    if value is None:
        return '—'
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime.combine(value, datetime.min.time(), tzinfo=timezone.utc)
    else:
        text = str(value).strip()
        if not text:
            return '—'
        if text.endswith('Z'):
            text = text[:-1] + '+00:00'
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            try:
                return _format_date(text)
            except ValueError:
                return text
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime('%B %d, %Y at %I:%M %p %Z')


def _resolve_data_dir():
    from user_auth import get_auth_data_dir
    return get_auth_data_dir()


def _recipient_for_user(user_id):
    from user_auth import _fetch_user_by_id
    user = _fetch_user_by_id(user_id)
    if not user:
        return None
    email = (user.get('email') or '').strip()
    return email or None


def _deliver(user_id, send_callable):
    try:
        to_email = _recipient_for_user(user_id)
        if not to_email:
            return
        config = merge_email_config(data_dir=_resolve_data_dir())
        if not is_email_configured(config):
            return
        send_callable(config, to_email)
    except Exception:
        return


def notify_monthly_subscription_activated(user_id, subscription=None, plan_name=None, start_date=None, end_date=None, source='Stripe'):
    sub = subscription or {}
    plan = plan_name or sub.get('planName') or sub.get('plan_name') or 'Monthly subscription'
    start = start_date or sub.get('startDate') or sub.get('start_date')
    end = end_date or sub.get('endDate') or sub.get('end_date')
    manage_url = _manage_subscription_url()
    purchase_source = source or 'Stripe'

    def _send(config, to_email):
        send_monthly_subscription_activation_email(
            config,
            to_email,
            plan,
            _format_date(start),
            _format_date(end),
            source=purchase_source,
            manage_url=manage_url,
        )

    _deliver(user_id, _send)


def notify_one_day_pass_activated(user_id, subscription=None, plan_name=None, expires_at=None, source='Stripe'):
    sub = subscription or {}
    plan = plan_name or sub.get('displayPlanName') or sub.get('planName') or sub.get('plan_name') or 'One Day Pass'
    expires = expires_at or sub.get('expiresAt') or sub.get('expires_at') or sub.get('endDate') or sub.get('end_date')
    manage_url = _manage_subscription_url()
    purchase_source = source or 'Stripe'

    def _send(config, to_email):
        send_one_day_pass_activation_email(
            config,
            to_email,
            plan,
            _format_datetime(expires),
            source=purchase_source,
            manage_url=manage_url,
        )

    _deliver(user_id, _send)


def notify_promo_access_activated(user_id, promo_code, duration_days, subscription=None, expires_at=None):
    sub = subscription or {}
    expires = expires_at or sub.get('expiresAt') or sub.get('expires_at') or sub.get('endDate') or sub.get('end_date')
    manage_url = _manage_subscription_url()

    def _send(config, to_email):
        send_promo_access_activation_email(
            config,
            to_email,
            promo_code,
            int(duration_days or 0),
            _format_datetime(expires),
            manage_url=manage_url,
        )

    _deliver(user_id, _send)


def notify_subscription_cancellation_scheduled(user_id, subscription=None, plan_name=None, end_date=None):
    sub = subscription or {}
    plan = plan_name or sub.get('displayPlanName') or sub.get('planName') or sub.get('plan_name') or 'Monthly subscription'
    end = end_date or sub.get('endDate') or sub.get('end_date')
    manage_url = _manage_subscription_url()

    def _send(config, to_email):
        send_subscription_cancellation_email(
            config,
            to_email,
            plan,
            _format_date(end),
            manage_url=manage_url,
        )

    _deliver(user_id, _send)


def maybe_notify_subscription_cancellation_scheduled(user_id, subscription, was_cancel_pending):
    if was_cancel_pending:
        return
    if not subscription or not subscription.get('cancel_at_period_end'):
        return
    notify_subscription_cancellation_scheduled(user_id, subscription)
