(function () {
    'use strict';

    const planButtons = document.querySelectorAll('.subscribe-plan-btn');
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

            const sub = data.activeSubscription;
            if (sub && sub.passType !== 'one_day' && sub.stripeSubscriptionId && portalBtn) {
                portalBtn.style.display = 'inline-flex';
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
