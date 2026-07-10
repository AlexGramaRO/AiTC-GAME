(function () {
    'use strict';

    const planButtons = document.querySelectorAll('.subscribe-plan-btn');
    const manageBtn = document.getElementById('subscribeManageBtn');
    const cancelBtn = document.getElementById('subscribeCancelBtn');
    const portalBtn = document.getElementById('subscribePortalBtn');
    const homeBtn = document.getElementById('subscribeHomeBtn');
    const logoutBtn = document.getElementById('subscribeLogoutBtn');
    const messageEl = document.getElementById('subscribeMessage');
    const successBanner = document.getElementById('subscribeSuccessBanner');

    function setMessage(text, kind) {
        if (!messageEl) return;
        messageEl.textContent = text || '';
        messageEl.style.display = text ? 'block' : 'none';
        messageEl.className = 'auth-message' + (kind ? ' auth-message-' + kind : '');
    }

    async function postJson(url, body) {
        const resp = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: body ? JSON.stringify(body) : undefined,
        });
        let data = {};
        try {
            data = await resp.json();
        } catch (_) {
            data = {};
        }
        return { resp, data };
    }

    function formatDate(value) {
        if (!value) return 'the end of your billing period';
        const d = new Date(value);
        if (Number.isNaN(d.getTime())) return value;
        return d.toLocaleDateString();
    }

    async function refreshStatus() {
        try {
            const resp = await fetch('/api/billing/status');
            const data = await resp.json();
            if (!resp.ok || !data.ok) return data;

            if (data.canAccessPlatform) {
                if (homeBtn) homeBtn.style.display = 'inline-flex';
                if (successBanner) {
                    successBanner.textContent = 'Access active. You can use the simulator now.';
                }
                if (window.AITC_SUBSCRIBE && window.AITC_SUBSCRIBE.success) {
                    window.setTimeout(function () {
                        window.location.href = '/';
                    }, 1500);
                }
            }

            if (manageBtn) {
                manageBtn.style.display = 'inline-flex';
            }

            const monthly = data.activeMonthlySubscription;
            if (cancelBtn) {
                cancelBtn.style.display = (data.canCancelSubscription && monthly && monthly.source === 'stripe')
                    ? 'inline-flex'
                    : 'none';
            }
            if (portalBtn) {
                portalBtn.style.display = (data.canCancelViaStripe && data.activeMonthlySubscription)
                    ? 'inline-flex'
                    : 'none';
            }
            return data;
        } catch (_) {
            return null;
        }
    }

    planButtons.forEach(function (btn) {
        btn.addEventListener('click', async function () {
            const planType = btn.getAttribute('data-plan-type') || 'monthly';
            setMessage('');
            btn.disabled = true;
            try {
                const { resp, data } = await postJson('/api/billing/create-checkout-session', { planType: planType });
                if (!resp.ok || !data.ok || !data.url) {
                    setMessage(data.error || 'Could not start checkout.', 'error');
                    return;
                }
                window.location.href = data.url;
            } catch (_) {
                setMessage('Network error. Try again.', 'error');
            } finally {
                btn.disabled = false;
            }
        });
    });

    cancelBtn?.addEventListener('click', async function () {
        setMessage('');
        let endDate = 'the end of your billing period';
        try {
            const statusResp = await fetch('/api/billing/status');
            const statusData = await statusResp.json();
            endDate = formatDate(statusData?.activeMonthlySubscription?.endDate);
        } catch (_) {}

        if (!window.confirm(
            'Cancel auto-renewal at the end of your current billing period?\n\n'
            + 'You will keep full access until '
            + endDate
            + ', and you will not be charged again.'
        )) {
            return;
        }

        cancelBtn.disabled = true;
        try {
            const { resp, data } = await postJson('/api/billing/cancel-subscription');
            if (!resp.ok || !data.ok) {
                setMessage(data.error || 'Could not cancel subscription.', 'error');
                return;
            }
            setMessage(data.message || 'Subscription cancellation scheduled.', 'info');
            await refreshStatus();
        } catch (_) {
            setMessage('Network error. Try again.', 'error');
        } finally {
            cancelBtn.disabled = false;
        }
    });

    portalBtn?.addEventListener('click', async function () {
        setMessage('');
        portalBtn.disabled = true;
        try {
            const { resp, data } = await postJson('/api/billing/customer-portal');
            if (!resp.ok || !data.ok || !data.url) {
                setMessage(data.error || 'Could not open billing portal.', 'error');
                return;
            }
            window.location.href = data.url;
        } catch (_) {
            setMessage('Network error. Try again.', 'error');
        } finally {
            portalBtn.disabled = false;
        }
    });

    logoutBtn?.addEventListener('click', async function () {
        await fetch('/api/auth/logout', { method: 'POST' });
        window.location.href = '/login';
    });

    refreshStatus();
    if (window.AITC_SUBSCRIBE && window.AITC_SUBSCRIBE.success) {
        let attempts = 0;
        const poll = window.setInterval(async function () {
            attempts += 1;
            const data = await refreshStatus();
            if (data && data.canAccessPlatform) {
                window.clearInterval(poll);
                return;
            }
            if (attempts >= 20) {
                window.clearInterval(poll);
                setMessage('Payment is processing. Refresh this page in a moment or click Go to simulator.', 'info');
                if (homeBtn) homeBtn.style.display = 'inline-flex';
            }
        }, 2000);
    }
})();
