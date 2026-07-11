(function () {
    'use strict';

    const tableBody = document.getElementById('adminPromotionsTableBody');
    const durationInput = document.getElementById('promoDurationDays');
    const maxUsesInput = document.getElementById('promoMaxUses');
    const generateBtn = document.getElementById('generatePromoCodeBtn');
    const refreshBtn = document.getElementById('adminPromotionsRefreshBtn');
    const logoutBtn = document.getElementById('adminPromotionsLogoutBtn');
    const messageEl = document.getElementById('adminPromotionsMessage');

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
        return d.toLocaleString();
    }

    function renderPromotions(promotions) {
        if (!tableBody) return;
        if (!Array.isArray(promotions) || !promotions.length) {
            tableBody.innerHTML = '<tr><td colspan="5" class="hint-text">No promotion codes yet.</td></tr>';
            return;
        }

        tableBody.innerHTML = promotions.map(function (promo) {
            const code = (promo.code || '').toString();
            const uses = `${promo.useCount || 0} / ${promo.maxUses || 0}`;
            const remaining = promo.usesRemaining != null ? promo.usesRemaining : '—';
            return `<tr>
                <td><code class="promo-code-chip">${code}</code></td>
                <td>${promo.durationDays || 0}</td>
                <td>${uses}</td>
                <td>${remaining}</td>
                <td>${formatDate(promo.createdAt)}</td>
            </tr>`;
        }).join('');
    }

    async function loadPromotions() {
        setMessage('');
        try {
            const resp = await fetch('/api/admin/promotions');
            const data = await resp.json().catch(function () { return {}; });
            if (!resp.ok || !data.ok) {
                throw new Error(data.error || 'Could not load promotions.');
            }
            renderPromotions(data.promotions || []);
        } catch (err) {
            setMessage(err.message || 'Could not load promotions.', 'error');
            renderPromotions([]);
        }
    }

    async function generatePromotion() {
        const durationDays = parseInt(durationInput?.value, 10);
        const maxUses = parseInt(maxUsesInput?.value, 10);
        if (!durationDays || durationDays < 1) {
            setMessage('Enter at least 1 day of access.', 'error');
            return;
        }
        if (!maxUses || maxUses < 1) {
            setMessage('Enter at least 1 maximum use.', 'error');
            return;
        }

        generateBtn.disabled = true;
        setMessage('');
        try {
            const resp = await fetch('/api/admin/promotions', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ durationDays: durationDays, maxUses: maxUses }),
            });
            const data = await resp.json().catch(function () { return {}; });
            if (!resp.ok || !data.ok) {
                throw new Error(data.error || 'Could not generate promotion code.');
            }
            const code = data.promotion?.code || '';
            setMessage(code ? `Generated code: ${code}` : 'Promotion code generated.', 'success');
            await loadPromotions();
        } catch (err) {
            setMessage(err.message || 'Could not generate promotion code.', 'error');
        } finally {
            generateBtn.disabled = false;
        }
    }

    generateBtn?.addEventListener('click', generatePromotion);
    refreshBtn?.addEventListener('click', loadPromotions);
    logoutBtn?.addEventListener('click', async function () {
        await fetch('/api/auth/logout', { method: 'POST' });
        window.location.href = '/login';
    });

    loadPromotions();
})();
