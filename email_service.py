"""
SMTP email delivery for sign-up verification and other platform mail.

Configuration comes from Admin → Configure email (admin_settings.json) with optional
Railway/environment overrides (SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, etc.).
"""

import json
import os
import smtplib
import ssl
from email.message import EmailMessage


DEFAULT_EMAIL_CONFIG = {
    'enabled': False,
    'smtpHost': '',
    'smtpPort': 587,
    'smtpSecurity': 'starttls',  # none | starttls | ssl
    'smtpUser': '',
    'smtpPassword': '',
    'fromEmail': '',
    'fromName': 'webATC',
}


def _coerce_port(value, default=587):
    try:
        port = int(value)
    except (TypeError, ValueError):
        return default
    if port < 1 or port > 65535:
        return default
    return port


def _normalize_security(value):
    security = (value or 'starttls').strip().lower()
    if security in ('none', 'starttls', 'ssl'):
        return security
    return 'starttls'


def _read_admin_email_config(data_dir):
    """Read emailConfig from DB-backed admin settings, with legacy file fallback."""
    try:
        from platform_settings_store import load_admin_settings_document
        data = load_admin_settings_document(data_dir)
        if isinstance(data, dict):
            raw = data.get('emailConfig')
            if isinstance(raw, dict):
                return raw
    except Exception:
        pass

    path = os.path.join(data_dir or 'data', 'admin_settings.json')
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    raw = data.get('emailConfig') if isinstance(data, dict) else None
    return raw if isinstance(raw, dict) else {}


def merge_email_config(file_config=None, data_dir=None):
    """Merge admin settings with SMTP_* environment variables."""
    file_config = file_config if isinstance(file_config, dict) else _read_admin_email_config(data_dir)
    merged = dict(DEFAULT_EMAIL_CONFIG)
    merged.update({k: v for k, v in file_config.items() if k in DEFAULT_EMAIL_CONFIG})

    env_map = {
        'smtpHost': os.environ.get('SMTP_HOST', ''),
        'smtpPort': os.environ.get('SMTP_PORT', ''),
        'smtpUser': os.environ.get('SMTP_USER', ''),
        'smtpPassword': os.environ.get('SMTP_PASSWORD', ''),
        'fromEmail': os.environ.get('SMTP_FROM_EMAIL', '') or os.environ.get('SMTP_FROM', ''),
        'fromName': os.environ.get('SMTP_FROM_NAME', ''),
        'smtpSecurity': os.environ.get('SMTP_SECURITY', ''),
    }
    for key, value in env_map.items():
        if isinstance(value, str) and value.strip():
            merged[key] = value.strip()

    if os.environ.get('SMTP_ENABLED', '').strip().lower() in ('1', 'true', 'yes', 'on'):
        merged['enabled'] = True

    merged['smtpPort'] = _coerce_port(merged.get('smtpPort'), 587)
    merged['smtpSecurity'] = _normalize_security(merged.get('smtpSecurity'))
    merged['enabled'] = bool(merged.get('enabled'))
    merged['smtpHost'] = (merged.get('smtpHost') or '').strip()
    merged['smtpUser'] = (merged.get('smtpUser') or '').strip()
    merged['smtpPassword'] = (merged.get('smtpPassword') or '').strip()
    merged['fromEmail'] = (merged.get('fromEmail') or '').strip()
    merged['fromName'] = (merged.get('fromName') or 'webATC').strip() or 'webATC'
    return merged


def normalize_email_config_for_storage(raw):
    current = raw if isinstance(raw, dict) else {}
    stored = {
        'enabled': bool(current.get('enabled')),
        'smtpHost': (current.get('smtpHost') or '').strip(),
        'smtpPort': _coerce_port(current.get('smtpPort'), 587),
        'smtpSecurity': _normalize_security(current.get('smtpSecurity')),
        'smtpUser': (current.get('smtpUser') or '').strip(),
        'fromEmail': (current.get('fromEmail') or '').strip(),
        'fromName': (current.get('fromName') or 'webATC').strip() or 'webATC',
    }
    password = current.get('smtpPassword')
    if isinstance(password, str) and password.strip():
        stored['smtpPassword'] = password.strip()
    elif 'smtpPassword' in current and current.get('smtpPassword') is not None:
        stored['smtpPassword'] = (current.get('smtpPassword') or '').strip()
    return stored


def email_config_for_admin_ui(file_config=None, data_dir=None):
    merged = merge_email_config(file_config, data_dir)
    return {
        'enabled': merged.get('enabled', False),
        'smtpHost': merged.get('smtpHost', ''),
        'smtpPort': merged.get('smtpPort', 587),
        'smtpSecurity': merged.get('smtpSecurity', 'starttls'),
        'smtpUser': merged.get('smtpUser', ''),
        'fromEmail': merged.get('fromEmail', ''),
        'fromName': merged.get('fromName', 'webATC'),
        'smtpPasswordConfigured': bool(merged.get('smtpPassword')),
        'configured': is_email_configured(merged),
        'fromEnv': bool(os.environ.get('SMTP_HOST', '').strip()),
    }


def is_email_configured(config=None, data_dir=None):
    cfg = config if config is not None else merge_email_config(data_dir=data_dir)
    if not cfg.get('enabled'):
        return False
    if not cfg.get('smtpHost') or not cfg.get('fromEmail'):
        return False
    if cfg.get('smtpUser') and not cfg.get('smtpPassword'):
        return False
    return True


def send_email(config, to_email, subject, text_body, html_body=None):
    cfg = merge_email_config(config)
    if not is_email_configured(cfg):
        raise RuntimeError('Email is not configured')

    to_email = (to_email or '').strip()
    if not to_email:
        raise RuntimeError('Recipient email is required')

    from_email = cfg['fromEmail']
    from_name = cfg['fromName']
    message = EmailMessage()
    message['Subject'] = subject
    message['From'] = f'{from_name} <{from_email}>' if from_name else from_email
    message['To'] = to_email
    message.set_content(text_body)
    if html_body:
        message.add_alternative(html_body, subtype='html')

    host = cfg['smtpHost']
    port = cfg['smtpPort']
    security = cfg['smtpSecurity']
    username = cfg.get('smtpUser') or ''
    password = cfg.get('smtpPassword') or ''

    if security == 'ssl':
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as smtp:
            if username:
                smtp.login(username, password)
            smtp.send_message(message)
        return

    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.ehlo()
        if security == 'starttls':
            context = ssl.create_default_context()
            smtp.starttls(context=context)
            smtp.ehlo()
        if username:
            smtp.login(username, password)
        smtp.send_message(message)


def send_signup_verification_email(config, to_email, code):
    subject = 'Your webATC verification code'
    text_body = (
        f'Your webATC verification code is: {code}\n\n'
        'Enter this code in the sign-up window within 5 minutes.\n\n'
        'If you did not request this, you can ignore this email.'
    )
    html_body = (
        f'<p>Your webATC verification code is:</p>'
        f'<p style="font-size:28px;font-weight:700;letter-spacing:0.25em;">{code}</p>'
        f'<p>Enter this code in the sign-up window within <strong>5 minutes</strong>.</p>'
        f'<p>If you did not request this, you can ignore this email.</p>'
    )
    send_email(config, to_email, subject, text_body, html_body)


def _billing_email_html(title, paragraphs, manage_url=None):
    parts = [f'<p style="margin:0 0 12px;">{p}</p>' for p in paragraphs]
    if manage_url:
        parts.append(
            f'<p style="margin:16px 0 0;"><a href="{manage_url}">Manage your subscription</a></p>'
        )
    body = ''.join(parts)
    return (
        f'<div style="font-family:Arial,sans-serif;line-height:1.5;color:#1a1a1a;">'
        f'<h2 style="margin:0 0 16px;font-size:20px;">{title}</h2>'
        f'{body}'
        f'<p style="margin:20px 0 0;color:#666;font-size:13px;">webATC platform</p>'
        f'</div>'
    )


def send_monthly_subscription_activation_email(config, to_email, plan_name, start_date, end_date, source='Stripe', manage_url=None):
    subject = f'Your {plan_name} is active — webATC'
    text_body = (
        f'Your {plan_name} is now active on webATC.\n\n'
        f'Access period: {start_date} to {end_date}\n'
        f'Purchased via: {source}\n\n'
        'You can use the simulator for the full paid period. '
        'Monthly plans renew automatically unless you cancel before the next billing date.\n'
    )
    if manage_url:
        text_body += f'\nManage your subscription: {manage_url}\n'
    html_body = _billing_email_html(
        'Monthly subscription confirmed',
        [
            f'Your <strong>{plan_name}</strong> is now active on webATC.',
            f'Access period: <strong>{start_date}</strong> to <strong>{end_date}</strong>.',
            f'Purchased via: <strong>{source}</strong>.',
            'You can use the simulator for the full paid period. '
            'Monthly plans renew automatically unless you cancel before the next billing date.',
        ],
        manage_url,
    )
    send_email(config, to_email, subject, text_body, html_body)


def send_one_day_pass_activation_email(config, to_email, plan_name, expires_at, source='Stripe', manage_url=None):
    subject = f'Your {plan_name} is active — webATC'
    text_body = (
        f'Your {plan_name} is now active on webATC.\n\n'
        f'Access expires: {expires_at}\n'
        f'Purchased via: {source}\n\n'
        'Your pass provides 24 hours of platform access from activation and does not auto-renew.\n'
    )
    if manage_url:
        text_body += f'\nManage your access: {manage_url}\n'
    html_body = _billing_email_html(
        'One Day Pass confirmed',
        [
            f'Your <strong>{plan_name}</strong> is now active on webATC.',
            f'Access expires: <strong>{expires_at}</strong>.',
            f'Purchased via: <strong>{source}</strong>.',
            'Your pass provides 24 hours of platform access from activation and does not auto-renew.',
        ],
        manage_url,
    )
    send_email(config, to_email, subject, text_body, html_body)


def send_promo_access_activation_email(config, to_email, promo_code, duration_days, expires_at, manage_url=None):
    subject = 'Promotion code applied — webATC'
    text_body = (
        f'Your promotion code {promo_code} has been applied on webATC.\n\n'
        f'Access added: {duration_days} day(s)\n'
        f'Access expires: {expires_at}\n\n'
        'Promo access does not auto-renew.\n'
    )
    if manage_url:
        text_body += f'\nManage your access: {manage_url}\n'
    html_body = _billing_email_html(
        'Promotion code applied',
        [
            f'Your promotion code <strong>{promo_code}</strong> has been applied.',
            f'Access added: <strong>{duration_days} day(s)</strong>.',
            f'Access expires: <strong>{expires_at}</strong>.',
            'Promo access does not auto-renew.',
        ],
        manage_url,
    )
    send_email(config, to_email, subject, text_body, html_body)


def send_subscription_cancellation_email(config, to_email, plan_name, end_date, manage_url=None):
    subject = f'{plan_name} cancellation confirmed — webATC'
    text_body = (
        f'Your {plan_name} auto-renewal has been cancelled on webATC.\n\n'
        f'You keep full access until: {end_date}\n\n'
        'You will not be charged again for the next billing period.\n'
    )
    if manage_url:
        text_body += f'\nManage your subscription: {manage_url}\n'
    html_body = _billing_email_html(
        'Subscription cancellation confirmed',
        [
            f'Your <strong>{plan_name}</strong> auto-renewal has been cancelled.',
            f'You keep full access until <strong>{end_date}</strong>.',
            'You will not be charged again for the next billing period.',
        ],
        manage_url,
    )
    send_email(config, to_email, subject, text_body, html_body)
