(function () {
    'use strict';

    window.initPromoCodeRedemption = function initPromoCodeRedemption(options) {
        const input = document.getElementById(options?.inputId || 'promoCodeInput');
        const button = document.getElementById(options?.buttonId || 'redeemPromoCodeBtn');
        const messageEl = document.getElementById(options?.messageId || 'promoCodeMessage');
        const onSuccess = typeof options?.onSuccess === 'function' ? options.onSuccess : null;

        function setMessage(text, kind) {
            if (!messageEl) return;
            messageEl.textContent = text || '';
            messageEl.style.display = text ? 'block' : 'none';
            messageEl.className = 'auth-message' + (kind ? ' auth-message-' + kind : '');
        }

        function normalizeInput(value) {
            return (value || '').toUpperCase().replace(/[^A-Z0-9]/g, '').slice(0, 10);
        }

        input?.addEventListener('input', function () {
            const normalized = normalizeInput(input.value);
            if (input.value !== normalized) input.value = normalized;
        });

        async function redeem() {
            const code = normalizeInput(input?.value || '');
            if (code.length !== 10) {
                setMessage('Enter a 10-character promotion code.', 'error');
                return;
            }

            button.disabled = true;
            setMessage('');
            try {
                const resp = await fetch('/api/billing/redeem-promo', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ code: code }),
                });
                const data = await resp.json().catch(function () { return {}; });
                if (!resp.ok || !data.ok) {
                    setMessage(data.error || 'Could not apply promotion code.', 'error');
                    return;
                }
                setMessage(data.message || 'Promotion applied.', 'success');
                if (input) input.value = '';
                if (onSuccess) onSuccess(data);
            } catch (_) {
                setMessage('Network error. Try again.', 'error');
            } finally {
                button.disabled = false;
            }
        }

        button?.addEventListener('click', redeem);
        input?.addEventListener('keydown', function (event) {
            if (event.key === 'Enter') {
                event.preventDefault();
                redeem();
            }
        });
    };
})();
