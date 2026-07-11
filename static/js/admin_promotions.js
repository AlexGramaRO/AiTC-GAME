(function () {
    'use strict';

    const tableBody = document.getElementById('adminPromotionsTableBody');
    const durationInput = document.getElementById('promoDurationDays');
    const maxUsesInput = document.getElementById('promoMaxUses');
    const generateBtn = document.getElementById('generatePromoCodeBtn');
    const refreshBtn = document.getElementById('adminPromotionsRefreshBtn');
    const logoutBtn = document.getElementById('adminPromotionsLogoutBtn');
    const messageEl = document.getElementById('adminPromotionsMessage');
    const storageHintEl = document.getElementById('adminPromotionsStorageHint');

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

    function renderStorageHint(storage) {
        if (!storageHintEl) return;
        if (storage === 'postgresql') {
            storageHintEl.textContent = 'Codes and usage are stored in your Railway PostgreSQL database and survive redeploys.';
        } else {
            storageHintEl.textContent = 'Warning: codes are stored in local SQLite on this server. Link Railway PostgreSQL via DATABASE_URL so codes survive redeploys.';
            storageHintEl.classList.add('admin-promotions-storage-warn');
        }
    }

    function renderPromotions(promotions) {
        if (!tableBody) return;
        if (!Array.isArray(promotions) || !promotions.length) {
            tableBody.innerHTML = '<tr><td colspan="7" class="hint-text">No promotion codes yet.</td></tr>';
            return;
        }

        tableBody.innerHTML = promotions.map(function (promo) {
            const code = (promo.code || '').toString();
            const uses = `${promo.useCount || 0} / ${promo.maxUses || 0}`;
            const remaining = promo.usesRemaining != null ? promo.usesRemaining : '—';
            const isActive = promo.isActive !== false;
            const statusClass = isActive ? 'promo-status-active' : 'promo-status-stopped';
            const statusLabel = isActive ? 'Active' : 'Stopped';
            const stopResumeBtn = isActive
                ? `<button type="button" class="btn btn-secondary admin-promo-action-btn" data-promo-action="stop" data-promo-id="${promo.id}">Stop usage</button>`
                : `<button type="button" class="btn btn-secondary admin-promo-action-btn" data-promo-action="resume" data-promo-id="${promo.id}">Resume</button>`;
            return `<tr data-promo-id="${promo.id}">
                <td><code class="promo-code-chip">${code}</code></td>
                <td>${promo.durationDays || 0}</td>
                <td>${uses}</td>
                <td>${remaining}</td>
                <td><span class="promo-status-pill ${statusClass}">${statusLabel}</span></td>
                <td>${formatDate(promo.createdAt)}</td>
                <td class="admin-promotions-actions-cell">
                    ${stopResumeBtn}
                    <button type="button" class="btn btn-tertiary admin-promo-action-btn" data-promo-action="delete" data-promo-id="${promo.id}" data-promo-code="${code}">Delete</button>
                </td>
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
            renderStorageHint(data.storage);
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

    async function handlePromoAction(action, promotionId, promoCode) {
        if (action === 'delete') {
            if (!window.confirm(`Delete promotion code ${promoCode || ''}? This cannot be undone.`)) {
                return;
            }
            const resp = await fetch(`/api/admin/promotions/${encodeURIComponent(promotionId)}`, {
                method: 'DELETE',
            });
            const data = await resp.json().catch(function () { return {}; });
            if (!resp.ok || !data.ok) {
                throw new Error(data.error || 'Could not delete promotion code.');
            }
            setMessage(`Deleted code ${promoCode || ''}.`, 'success');
            await loadPromotions();
            return;
        }

        const endpoint = action === 'stop' ? 'stop' : 'resume';
        const resp = await fetch(`/api/admin/promotions/${encodeURIComponent(promotionId)}/${endpoint}`, {
            method: 'POST',
        });
        const data = await resp.json().catch(function () { return {}; });
        if (!resp.ok || !data.ok) {
            throw new Error(data.error || `Could not ${action} promotion code.`);
        }
        setMessage(action === 'stop' ? 'Promotion code stopped.' : 'Promotion code resumed.', 'success');
        await loadPromotions();
    }

    tableBody?.addEventListener('click', function (event) {
        const btn = event.target.closest('[data-promo-action]');
        if (!btn) return;
        const action = btn.getAttribute('data-promo-action');
        const promotionId = btn.getAttribute('data-promo-id');
        const promoCode = btn.getAttribute('data-promo-code') || '';
        if (!action || !promotionId) return;
        btn.disabled = true;
        handlePromoAction(action, promotionId, promoCode).catch(function (err) {
            setMessage(err.message || 'Action failed.', 'error');
        }).finally(function () {
            btn.disabled = false;
        });
    });

    generateBtn?.addEventListener('click', generatePromotion);
    refreshBtn?.addEventListener('click', loadPromotions);
    logoutBtn?.addEventListener('click', async function () {
        await fetch('/api/auth/logout', { method: 'POST' });
        window.location.href = '/login';
    });

    loadPromotions();
})();
