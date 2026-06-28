"""
Stripe Checkout + webhooks for AiTC subscriptions.

Railway environment variables:
  STRIPE_SECRET_KEY              — Stripe secret key (sk_live_... or sk_test_...)
  STRIPE_PUBLISHABLE_KEY         — Stripe publishable key (pk_...)
  STRIPE_WEBHOOK_SECRET          — Webhook signing secret (whsec_...)
  STRIPE_PRICE_ID                — Recurring monthly price (31-day interval) — price_...
  STRIPE_PLAN_NAME                 — Display name for monthly plan (optional)
  STRIPE_ONE_DAY_PRICE_ID          — One-time One Day Pass price — price_...
  STRIPE_ONE_DAY_PLAN_NAME         — Display name for One Day Pass (optional)
  APP_BASE_URL                     — Public app URL, e.g. https://your-app.up.railway.app
"""

import os
from datetime import date, datetime, timezone

from flask import Blueprint, jsonify, redirect, render_template, request, url_for

from user_auth import (
    PASS_TYPE_ONE_DAY,
    cancel_subscription_by_stripe_id,
    create_one_day_pass,
    fetch_subscription_by_stripe_id,
    fetch_user_by_stripe_customer_id,
    get_current_user,
    set_user_stripe_customer_id,
    subscription_end_date,
    upsert_stripe_subscription,
    user_can_access_platform,
    user_is_approved,
    _fetch_active_subscription,
    _subscription_to_api,
    _user_to_api,
)

billing_bp = Blueprint('stripe_billing', __name__)

_stripe = None


def _stripe_client():
    global _stripe
    secret = (os.environ.get('STRIPE_SECRET_KEY') or '').strip()
    if not secret:
        return None
    if _stripe is None:
        import stripe
        stripe.api_key = secret
        _stripe = stripe
    return _stripe


def _monthly_price_id():
    return (os.environ.get('STRIPE_PRICE_ID') or '').strip()


def _one_day_price_id():
    return (os.environ.get('STRIPE_ONE_DAY_PRICE_ID') or '').strip()


def stripe_monthly_configured():
    return bool((os.environ.get('STRIPE_SECRET_KEY') or '').strip() and _monthly_price_id())


def stripe_one_day_configured():
    return bool((os.environ.get('STRIPE_SECRET_KEY') or '').strip() and _one_day_price_id())


def stripe_configured():
    return stripe_monthly_configured() or stripe_one_day_configured()


def _monthly_plan_name():
    return (os.environ.get('STRIPE_PLAN_NAME') or 'AiTC Monthly').strip() or 'AiTC Monthly'


def _one_day_plan_name():
    return (os.environ.get('STRIPE_ONE_DAY_PLAN_NAME') or 'One Day Pass').strip() or 'One Day Pass'


def _app_base_url():
    base = (os.environ.get('APP_BASE_URL') or '').strip().rstrip('/')
    if base:
        return base
    host = request.host_url.rstrip('/') if request else ''
    return host


def _billing_products_payload():
    return {
        'monthly': {
            'available': stripe_monthly_configured(),
            'planName': _monthly_plan_name(),
            'billingPeriodDays': 31,
            'autoRenew': True,
        },
        'oneDay': {
            'available': stripe_one_day_configured(),
            'planName': _one_day_plan_name(),
            'accessHours': 24,
            'autoRenew': False,
        },
    }


def _stripe_period_dates(stripe_subscription):
    start_ts = stripe_subscription.get('current_period_start')
    end_ts = stripe_subscription.get('current_period_end')
    if start_ts and end_ts:
        start = datetime.fromtimestamp(start_ts, tz=timezone.utc).date()
        end = datetime.fromtimestamp(end_ts, tz=timezone.utc).date()
        return start, end
    start = date.today()
    return start, subscription_end_date(start)


def cancel_stripe_subscription(stripe_subscription_id):
    stripe = _stripe_client()
    if not stripe or not stripe_subscription_id:
        return False
    stripe.Subscription.cancel(stripe_subscription_id)
    cancel_subscription_by_stripe_id(stripe_subscription_id)
    return True


def _resolve_user_id_from_metadata(metadata, client_reference_id=None, customer_id=None):
    metadata = metadata or {}
    user_id = (metadata.get('user_id') or '').strip()
    if user_id:
        return user_id
    if client_reference_id:
        return str(client_reference_id).strip() or None
    if customer_id:
        user = fetch_user_by_stripe_customer_id(customer_id)
        if user:
            return str(user['id'])
    return None


def _sync_stripe_subscription(stripe_subscription):
    stripe_sub_id = stripe_subscription.get('id')
    if not stripe_sub_id:
        return None

    user_id = _resolve_user_id_from_metadata(
        stripe_subscription.get('metadata'),
        customer_id=stripe_subscription.get('customer'),
    )
    if not user_id:
        return None

    status = stripe_subscription.get('status') or ''
    if status in ('canceled', 'unpaid', 'incomplete_expired'):
        cancel_subscription_by_stripe_id(stripe_sub_id)
        return fetch_subscription_by_stripe_id(stripe_sub_id)

    start, end = _stripe_period_dates(stripe_subscription)
    plan_name = _monthly_plan_name()
    return upsert_stripe_subscription(user_id, stripe_sub_id, start, end, plan_name=plan_name)


def _handle_checkout_completed(session_obj):
    stripe = _stripe_client()
    if not stripe:
        return

    mode = session_obj.get('mode')
    metadata = session_obj.get('metadata') or {}
    user_id = _resolve_user_id_from_metadata(
        metadata,
        client_reference_id=session_obj.get('client_reference_id'),
        customer_id=session_obj.get('customer'),
    )
    customer_id = session_obj.get('customer')
    if customer_id and user_id:
        set_user_stripe_customer_id(user_id, customer_id)

    if mode == 'subscription':
        subscription_id = session_obj.get('subscription')
        if subscription_id:
            stripe_sub = stripe.Subscription.retrieve(subscription_id)
            _sync_stripe_subscription(stripe_sub)
        return

    if mode == 'payment':
        pass_type = (metadata.get('pass_type') or metadata.get('planType') or '').strip().lower()
        if pass_type not in (PASS_TYPE_ONE_DAY, 'one_day', 'oneday'):
            pass_type = PASS_TYPE_ONE_DAY
        if pass_type == PASS_TYPE_ONE_DAY and user_id:
            create_one_day_pass(
                user_id,
                plan_name=_one_day_plan_name(),
                notes='Stripe One Day Pass (24 hours of platform access)',
                stripe_checkout_session_id=session_obj.get('id'),
            )


def _ensure_stripe_customer(user):
    stripe = _stripe_client()
    if not stripe:
        raise RuntimeError('Stripe is not configured')

    existing = (user.get('stripe_customer_id') or '').strip()
    if existing:
        return existing

    customer = stripe.Customer.create(
        email=user['email'],
        name=user.get('display_name') or user['email'],
        metadata={'user_id': str(user['id'])},
    )
    set_user_stripe_customer_id(user['id'], customer.id)
    return customer.id


def init_stripe_billing(app):
    app.register_blueprint(billing_bp)


@billing_bp.route('/subscribe')
def subscribe_page():
    user = get_current_user()
    if not user:
        return redirect(url_for('user_auth.login_page', next='/subscribe'))
    if not user_is_approved(user):
        return redirect(url_for('user_auth.login_page', reason=user.get('status') or 'pending'))
    if user_can_access_platform(user):
        return redirect(url_for('index'))

    active = _fetch_active_subscription(user['id'])
    products = _billing_products_payload()
    return render_template(
        'subscribe.html',
        stripe_publishable_key=(os.environ.get('STRIPE_PUBLISHABLE_KEY') or '').strip(),
        stripe_configured=stripe_configured(),
        products=products,
        active_subscription=_subscription_to_api(active),
        cancelled=request.args.get('cancelled') == '1',
        success=False,
    )


@billing_bp.route('/subscribe/success')
def subscribe_success_page():
    user = get_current_user()
    if not user:
        return redirect(url_for('user_auth.login_page', next='/subscribe'))
    if user_can_access_platform(user):
        return redirect(url_for('index'))

    products = _billing_products_payload()
    return render_template(
        'subscribe.html',
        success=True,
        stripe_publishable_key=(os.environ.get('STRIPE_PUBLISHABLE_KEY') or '').strip(),
        stripe_configured=stripe_configured(),
        products=products,
        active_subscription=_subscription_to_api(_fetch_active_subscription(user['id'])),
        cancelled=False,
    )


@billing_bp.route('/api/billing/config', methods=['GET'])
def api_billing_config():
    products = _billing_products_payload()
    return jsonify({
        'ok': True,
        'configured': stripe_configured(),
        'publishableKey': (os.environ.get('STRIPE_PUBLISHABLE_KEY') or '').strip(),
        'products': products,
        'planName': products['monthly']['planName'],
        'billingPeriodDays': 31,
    })


@billing_bp.route('/api/billing/status', methods=['GET'])
def api_billing_status():
    user = get_current_user()
    if not user:
        return jsonify({'ok': False, 'error': 'Authentication required'}), 401
    active = _fetch_active_subscription(user['id'])
    return jsonify({
        'ok': True,
        'canAccessPlatform': user_can_access_platform(user),
        'activeSubscription': _subscription_to_api(active),
        'user': _user_to_api(user, include_subscription=True),
    })


@billing_bp.route('/api/billing/create-checkout-session', methods=['POST'])
def api_create_checkout_session():
    user = get_current_user()
    if not user:
        return jsonify({'ok': False, 'error': 'Authentication required'}), 401
    if not user_is_approved(user):
        return jsonify({'ok': False, 'error': 'Account must be approved before subscribing'}), 403
    if user.get('is_admin'):
        return jsonify({'ok': False, 'error': 'Admin accounts do not require a subscription'}), 400

    body = request.get_json(silent=True) or {}
    plan_type = (body.get('planType') or body.get('passType') or 'monthly').strip().lower()
    if plan_type in ('one_day', 'oneday', 'one-day', 'day'):
        plan_type = PASS_TYPE_ONE_DAY
    else:
        plan_type = 'monthly'

    stripe = _stripe_client()
    if not stripe:
        return jsonify({'ok': False, 'error': 'Stripe billing is not configured on this server'}), 503

    try:
        customer_id = _ensure_stripe_customer(user)
        base = _app_base_url()

        if plan_type == PASS_TYPE_ONE_DAY:
            price_id = _one_day_price_id()
            if not price_id:
                return jsonify({'ok': False, 'error': 'One Day Pass is not configured (STRIPE_ONE_DAY_PRICE_ID)'}), 503
            checkout_session = stripe.checkout.Session.create(
                mode='payment',
                customer=customer_id,
                client_reference_id=str(user['id']),
                line_items=[{'price': price_id, 'quantity': 1}],
                success_url=f'{base}/subscribe/success?session_id={{CHECKOUT_SESSION_ID}}',
                cancel_url=f'{base}/subscribe?cancelled=1',
                metadata={
                    'user_id': str(user['id']),
                    'pass_type': PASS_TYPE_ONE_DAY,
                    'plan_type': PASS_TYPE_ONE_DAY,
                },
                allow_promotion_codes=True,
            )
        else:
            price_id = _monthly_price_id()
            if not price_id:
                return jsonify({'ok': False, 'error': 'Monthly plan is not configured (STRIPE_PRICE_ID)'}), 503
            checkout_session = stripe.checkout.Session.create(
                mode='subscription',
                customer=customer_id,
                client_reference_id=str(user['id']),
                line_items=[{'price': price_id, 'quantity': 1}],
                success_url=f'{base}/subscribe/success?session_id={{CHECKOUT_SESSION_ID}}',
                cancel_url=f'{base}/subscribe?cancelled=1',
                metadata={'user_id': str(user['id']), 'plan_type': 'monthly'},
                subscription_data={
                    'metadata': {'user_id': str(user['id']), 'plan_type': 'monthly'},
                },
                allow_promotion_codes=True,
            )
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 502

    return jsonify({
        'ok': True,
        'url': checkout_session.url,
        'sessionId': checkout_session.id,
        'planType': plan_type,
    })


@billing_bp.route('/api/billing/customer-portal', methods=['POST'])
def api_customer_portal():
    """Stripe Customer Portal — cancel monthly subscription or update payment method."""
    user = get_current_user()
    if not user:
        return jsonify({'ok': False, 'error': 'Authentication required'}), 401

    stripe = _stripe_client()
    if not stripe:
        return jsonify({'ok': False, 'error': 'Stripe billing is not configured'}), 503

    customer_id = (user.get('stripe_customer_id') or '').strip()
    if not customer_id:
        try:
            customer_id = _ensure_stripe_customer(user)
        except Exception as exc:
            return jsonify({'ok': False, 'error': str(exc)}), 502

    try:
        portal = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=f'{_app_base_url()}/subscribe',
        )
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 502

    return jsonify({'ok': True, 'url': portal.url})


@billing_bp.route('/api/billing/stripe/webhook', methods=['POST'])
def api_stripe_webhook():
    stripe = _stripe_client()
    webhook_secret = (os.environ.get('STRIPE_WEBHOOK_SECRET') or '').strip()
    if not stripe or not webhook_secret:
        return jsonify({'ok': False, 'error': 'Stripe webhook is not configured'}), 503

    payload = request.get_data(as_text=True)
    sig_header = request.headers.get('Stripe-Signature', '')
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except ValueError:
        return jsonify({'ok': False, 'error': 'Invalid payload'}), 400
    except Exception:
        return jsonify({'ok': False, 'error': 'Invalid signature'}), 400

    event_type = event.get('type')
    data_object = (event.get('data') or {}).get('object') or {}

    try:
        if event_type == 'checkout.session.completed':
            _handle_checkout_completed(data_object)

        elif event_type in ('customer.subscription.created', 'customer.subscription.updated'):
            _sync_stripe_subscription(data_object)

        elif event_type == 'customer.subscription.deleted':
            stripe_sub_id = data_object.get('id')
            if stripe_sub_id:
                cancel_subscription_by_stripe_id(stripe_sub_id)

        elif event_type == 'invoice.paid':
            subscription_id = data_object.get('subscription')
            if subscription_id:
                stripe_sub = stripe.Subscription.retrieve(subscription_id)
                _sync_stripe_subscription(stripe_sub)

        elif event_type == 'invoice.payment_failed':
            subscription_id = data_object.get('subscription')
            if subscription_id:
                stripe_sub = stripe.Subscription.retrieve(subscription_id)
                if (stripe_sub.get('status') or '') in ('canceled', 'unpaid'):
                    cancel_subscription_by_stripe_id(subscription_id)
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 500

    return jsonify({'ok': True})
