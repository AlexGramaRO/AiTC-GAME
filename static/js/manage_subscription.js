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

    async function postJson(url) {
        const resp = await fetch(url, { method: 'POST' });
        let data = {};
        try {
            data = await resp.json();
        } catch (_) {
            data = {};
        }
        return { resp, data };
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
            let detail = sub.planName + ' · ' + formatDate(sub.startDate)
                + ' → ' + formatDate(sub.endDate) + ' · via ' + source;
            if (sub.cancelAtPeriodEnd) {
                detail += ' · auto-renewal off';
            }
            monthlyDetail.textContent = detail;
        }

        if (!monthlyActions) return;
        monthlyActions.innerHTML = '';

        if (sub.cancelAtPeriodEnd) {
            const banner = document.createElement('p');
            banner.className = 'auth-footnote';
            banner.style.marginTop = '0.75rem';
            banner.textContent = 'Cancellation scheduled. You keep full access until '
                + formatDate(sub.endDate) + ', and you will not be charged again.';
            monthlyActions.appendChild(banner);
        } else if (cfg.canCancelSubscription && sub.source === 'stripe') {
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'btn btn-secondary auth-submit';
            btn.id = 'cancelSubscriptionBtn';
            btn.textContent = 'Cancel subscription';
            btn.addEventListener('click', cancelSubscription);
            monthlyActions.appendChild(btn);

            const hint = document.createElement('p');
            hint.className = 'auth-footnote';
            hint.style.marginTop = '0.75rem';
            hint.textContent = 'Cancels auto-renewal at the end of your current billing period. You keep access until '
                + formatDate(sub.endDate) + '.';
            monthlyActions.appendChild(hint);
        } else if (sub.source !== 'stripe') {
            const hint = document.createElement('p');
            hint.className = 'auth-footnote';
            hint.style.marginTop = '0.75rem';
            hint.textContent = 'This monthly access was granted by an administrator. Contact an administrator to cancel it.';
            monthlyActions.appendChild(hint);
        }

        if (cfg.canCancelViaStripe && cfg.stripeConfigured) {
            const portalBtn = document.createElement('button');
            portalBtn.type = 'button';
            portalBtn.className = 'btn btn-secondary auth-submit';
            portalBtn.id = 'openStripePortalBtn';
            portalBtn.textContent = 'Update payment in Stripe';
            portalBtn.style.marginTop = '0.75rem';
            portalBtn.addEventListener('click', openStripePortal);
            monthlyActions.appendChild(portalBtn);
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

    async function cancelSubscription() {
        const sub = cfg.monthly;
        if (!sub || !window.confirm(
            'Cancel auto-renewal at the end of your current billing period?\n\n'
            + 'You will keep full access until '
            + formatDate(sub.endDate)
            + ', and you will not be charged again.'
        )) {
            return;
        }

        setMessage('');
        const btn = document.getElementById('cancelSubscriptionBtn');
        if (btn) btn.disabled = true;
        try {
            const { resp, data } = await postJson('/api/billing/cancel-subscription');
            if (!resp.ok || !data.ok) {
                setMessage(data.error || 'Could not cancel subscription.', 'error');
                return;
            }
            cfg.monthly = data.activeMonthlySubscription || cfg.monthly;
            cfg.canCancelSubscription = false;
            cfg.subscriptionPendingCancellation = true;
            if (cfg.monthly) {
                cfg.monthly.cancelAtPeriodEnd = true;
                cfg.monthly.autoRenew = false;
            }
            setMessage(data.message || 'Subscription cancellation scheduled.', 'info');
            renderMonthly();
        } catch (_) {
            setMessage('Network error. Try again.', 'error');
        } finally {
            if (btn) btn.disabled = false;
        }
    }

    async function openStripePortal() {
        setMessage('');
        const btn = document.getElementById('openStripePortalBtn');
        if (btn) btn.disabled = true;
        try {
            const { resp, data } = await postJson('/api/billing/customer-portal');
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

    if (window.initPromoCodeRedemption) {
        window.initPromoCodeRedemption({
            onSuccess: async function () {
                try {
                    const resp = await fetch('/api/billing/status');
                    const data = await resp.json().catch(function () { return {}; });
                    if (resp.ok && data.ok) {
                        cfg.monthly = data.activeMonthlySubscription || null;
                        cfg.oneDay = data.activeOneDayPass || null;
                        cfg.canCancelSubscription = !!data.canCancelSubscription;
                        cfg.canCancelViaStripe = !!data.canCancelViaStripe;
                        cfg.subscriptionPendingCancellation = !!data.subscriptionPendingCancellation;
                        renderMonthly();
                        renderOneDay();
                    }
                } catch (_) {}
            },
        });
    }

    logoutBtn?.addEventListener('click', async function () {
        await fetch('/api/auth/logout', { method: 'POST' });
        window.location.href = '/login';
    });
})();
