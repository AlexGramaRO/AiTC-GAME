(function () {
    'use strict';

    const monthlyDetail = document.getElementById('monthlyPlanDetail');
    const monthlyActions = document.getElementById('monthlyPlanActions');
    const oneDayDetail = document.getElementById('oneDayPlanDetail');
    const messageEl = document.getElementById('manageSubscriptionMessage');
    const logoutBtn = document.getElementById('manageLogoutBtn');
    const cfg = window.AITC_MANAGE || {};

    function setMessage(text, kind) {
        if (!messageEl) return;
        messageEl.textContent = text || '';
        messageEl.style.display = text ? 'block' : 'none';
        messageEl.className = 'auth-message' + (kind ? ' auth-message-' + kind : '');
    }

    function formatDate(value) {
        if (!value) return '—';
        const d = new Date(value);
        if (Number.isNaN(d.getTime())) return value;
        return d.toLocaleDateString();
    }

    function formatDateTime(value) {
        if (!value) return '—';
        const d = new Date(value);
        if (Number.isNaN(d.getTime())) return value;
        return d.toLocaleString();
    }

    function renderMonthly() {
        const sub = cfg.monthly;
        if (!sub) {
            if (monthlyDetail) {
                monthlyDetail.textContent = 'No active monthly subscription.';
            }
            return;
        }

        const source = sub.source === 'stripe' ? 'Stripe' : 'Administrator';
        if (monthlyDetail) {
            monthlyDetail.textContent = sub.planName + ' · ' + formatDate(sub.startDate)
                + ' → ' + formatDate(sub.endDate) + ' · via ' + source;
        }

        if (!monthlyActions) return;
        monthlyActions.innerHTML = '';

        if (cfg.canCancelViaStripe && cfg.stripeConfigured) {
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'btn btn-primary auth-submit';
            btn.id = 'openStripePortalBtn';
            btn.textContent = 'Cancel or update in Stripe';
            btn.addEventListener('click', openStripePortal);
            monthlyActions.appendChild(btn);

            const hint = document.createElement('p');
            hint.className = 'auth-footnote';
            hint.style.marginTop = '0.75rem';
            hint.textContent = 'Opens the secure Stripe billing portal where you can cancel auto-renewal or update your payment method.';
            monthlyActions.appendChild(hint);
        } else if (sub.source !== 'stripe') {
            const hint = document.createElement('p');
            hint.className = 'auth-footnote';
            hint.style.marginTop = '0.75rem';
            hint.textContent = 'This monthly access was granted by an administrator. Contact an administrator to cancel it.';
            monthlyActions.appendChild(hint);
        }
    }

    function renderOneDay() {
        const sub = cfg.oneDay;
        if (!sub) {
            if (oneDayDetail) {
                oneDayDetail.textContent = 'No active One Day Pass.';
            }
            return;
        }

        if (oneDayDetail) {
            oneDayDetail.textContent = sub.planName + ' · active until '
                + formatDateTime(sub.expiresAt || sub.endDate)
                + '. One Day Passes expire automatically after 24 hours and are not renewed.';
        }
    }

    async function openStripePortal() {
        setMessage('');
        const btn = document.getElementById('openStripePortalBtn');
        if (btn) btn.disabled = true;
        try {
            const resp = await fetch('/api/billing/customer-portal', { method: 'POST' });
            const data = await resp.json();
            if (!resp.ok || !data.ok || !data.url) {
                setMessage(data.error || 'Could not open Stripe billing portal.', 'error');
                return;
            }
            window.location.href = data.url;
        } catch (_) {
            setMessage('Network error. Try again.', 'error');
        } finally {
            if (btn) btn.disabled = false;
        }
    }

    renderMonthly();
    renderOneDay();

    logoutBtn?.addEventListener('click', async function () {
        await fetch('/api/auth/logout', { method: 'POST' });
        window.location.href = '/login';
    });
})();
