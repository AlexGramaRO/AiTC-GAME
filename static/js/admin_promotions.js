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

    const notifyModal = document.getElementById('promoNotifyModal');
    const notifyUsersList = document.getElementById('promoNotifyUsersList');
    const notifySelectedCount = document.getElementById('promoNotifySelectedCount');
    const notifySubject = document.getElementById('promoNotifySubject');
    const notifyMessage = document.getElementById('promoNotifyMessage');
    const notifyMessageEl = document.getElementById('promoNotifyMessageEl');
    const notifyTitle = document.getElementById('promoNotifyModalTitle');
    const notifySelectAllBtn = document.getElementById('promoNotifySelectAllBtn');
    const notifySendBtn = document.getElementById('promoNotifySendBtn');
    const notifyCloseBtn = document.getElementById('promoNotifyCloseBtn');
    const notifyCancelBtn = document.getElementById('promoNotifyCancelBtn');

    let notifyPromoId = null;
    let notifyPromoCode = '';
    let notifyUsersCache = [];
    let notifyAllSelected = false;

    function setMessage(text, kind) {
        if (!messageEl) return;
        messageEl.textContent = text || '';
        messageEl.style.display = text ? 'block' : 'none';
        messageEl.className = 'auth-message' + (kind ? ' auth-message-' + kind : '');
    }

    function setNotifyMessage(text, kind) {
        if (!notifyMessageEl) return;
        notifyMessageEl.textContent = text || '';
        notifyMessageEl.style.display = text ? 'block' : 'none';
        notifyMessageEl.className = 'auth-message' + (kind ? ' auth-message-' + kind : '');
    }

    function escapeHtml(value) {
        return String(value == null ? '' : value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
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
                <td><code class="promo-code-chip">${escapeHtml(code)}</code></td>
                <td>${promo.durationDays || 0}</td>
                <td>${uses}</td>
                <td>${remaining}</td>
                <td><span class="promo-status-pill ${statusClass}">${statusLabel}</span></td>
                <td>${formatDate(promo.createdAt)}</td>
                <td class="admin-promotions-actions-cell">
                    ${stopResumeBtn}
                    <button type="button" class="btn btn-primary admin-promo-action-btn" data-promo-action="notify" data-promo-id="${escapeHtml(promo.id)}" data-promo-code="${escapeHtml(code)}">Notify users</button>
                    <button type="button" class="btn btn-tertiary admin-promo-action-btn" data-promo-action="delete" data-promo-id="${escapeHtml(promo.id)}" data-promo-code="${escapeHtml(code)}">Delete</button>
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

    function openNotifyModal() {
        if (!notifyModal) return;
        notifyModal.classList.add('active');
        notifyModal.setAttribute('aria-hidden', 'false');
        document.body.classList.add('modal-open');
    }

    function closeNotifyModal() {
        if (!notifyModal) return;
        notifyModal.classList.remove('active');
        notifyModal.setAttribute('aria-hidden', 'true');
        document.body.classList.remove('modal-open');
        notifyPromoId = null;
        notifyPromoCode = '';
        notifyUsersCache = [];
        notifyAllSelected = false;
        if (notifySelectAllBtn) notifySelectAllBtn.textContent = 'Select all';
        setNotifyMessage('');
    }

    function updateSelectedCount() {
        if (!notifySelectedCount || !notifyUsersList) return;
        const checked = notifyUsersList.querySelectorAll('input[type="checkbox"][data-user-id]:checked').length;
        const total = notifyUsersList.querySelectorAll('input[type="checkbox"][data-user-id]').length;
        notifySelectedCount.textContent = `${checked} selected` + (total ? ` of ${total}` : '');
        notifyAllSelected = total > 0 && checked === total;
        if (notifySelectAllBtn) {
            notifySelectAllBtn.textContent = notifyAllSelected ? 'Deselect all' : 'Select all';
        }
    }

    function renderNotifyUsers(users) {
        if (!notifyUsersList) return;
        const withEmail = (users || []).filter(function (u) {
            return u && (u.email || '').toString().trim();
        });
        notifyUsersCache = withEmail;
        if (!withEmail.length) {
            notifyUsersList.innerHTML = '<p class="hint-text">No users with registered emails found.</p>';
            updateSelectedCount();
            return;
        }
        notifyUsersList.innerHTML = withEmail.map(function (user) {
            const id = String(user.id);
            const email = (user.email || '').toString();
            const name = (user.displayName || '').toString().trim();
            const status = (user.status || '').toString();
            const label = name ? `${email} (${name})` : email;
            return `<label class="promo-notify-user-row">
                <input type="checkbox" data-user-id="${escapeHtml(id)}" />
                <span class="promo-notify-user-meta">
                    <span class="promo-notify-user-email">${escapeHtml(label)}</span>
                    <span class="promo-notify-user-status">${escapeHtml(status || '—')}</span>
                </span>
            </label>`;
        }).join('');
        updateSelectedCount();
    }

    function getSelectedUserIds() {
        if (!notifyUsersList) return [];
        return Array.from(notifyUsersList.querySelectorAll('input[type="checkbox"][data-user-id]:checked'))
            .map(function (el) { return el.getAttribute('data-user-id'); })
            .filter(Boolean);
    }

    async function openNotifyUsers(promotionId, promoCode) {
        notifyPromoId = promotionId;
        notifyPromoCode = promoCode || '';
        notifyAllSelected = false;
        if (notifyTitle) {
            notifyTitle.textContent = promoCode
                ? `Notify users — ${promoCode}`
                : 'Notify users';
        }
        if (notifySubject) {
            notifySubject.value = 'Your webATC promotion code';
        }
        if (notifyMessage) {
            notifyMessage.value = [
                'Hello,',
                '',
                'You have been invited to try webATC.',
                'Use the promotion code <PROMOCODE> on the Subscribe / Manage subscription page to activate your access.',
                '',
                '— webATC',
            ].join('\n');
        }
        if (notifyUsersList) {
            notifyUsersList.innerHTML = '<p class="hint-text">Loading users…</p>';
        }
        setNotifyMessage('');
        openNotifyModal();

        try {
            const resp = await fetch('/api/admin/user-accounts');
            const data = await resp.json().catch(function () { return {}; });
            if (!resp.ok || !data.ok) {
                throw new Error(data.error || 'Could not load users.');
            }
            renderNotifyUsers(data.users || []);
        } catch (err) {
            if (notifyUsersList) {
                notifyUsersList.innerHTML = '<p class="hint-text">Could not load users.</p>';
            }
            setNotifyMessage(err.message || 'Could not load users.', 'error');
        }
    }

    async function sendNotifyEmails() {
        if (!notifyPromoId) return;
        const userIds = getSelectedUserIds();
        const subject = (notifySubject?.value || '').trim();
        const message = notifyMessage?.value || '';
        if (!userIds.length) {
            setNotifyMessage('Select at least one user.', 'error');
            return;
        }
        if (!subject) {
            setNotifyMessage('Enter a subject.', 'error');
            return;
        }
        if (!message.trim()) {
            setNotifyMessage('Enter a message.', 'error');
            return;
        }

        if (notifySendBtn) notifySendBtn.disabled = true;
        setNotifyMessage('Sending…');
        try {
            const resp = await fetch(`/api/admin/promotions/${encodeURIComponent(notifyPromoId)}/notify`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    userIds: userIds,
                    subject: subject,
                    message: message,
                }),
            });
            const data = await resp.json().catch(function () { return {}; });
            if (!resp.ok || !data.ok) {
                throw new Error(data.error || 'Could not send emails.');
            }
            const sent = data.sent || 0;
            const failed = Array.isArray(data.failed) ? data.failed.length : 0;
            const skipped = Array.isArray(data.skipped) ? data.skipped.length : 0;
            let summary = `Sent ${sent} email${sent === 1 ? '' : 's'}`;
            if (failed) summary += `, ${failed} failed`;
            if (skipped) summary += `, ${skipped} skipped`;
            summary += '.';
            closeNotifyModal();
            setMessage(summary, failed ? 'error' : 'success');
        } catch (err) {
            setNotifyMessage(err.message || 'Could not send emails.', 'error');
        } finally {
            if (notifySendBtn) notifySendBtn.disabled = false;
        }
    }

    async function handlePromoAction(action, promotionId, promoCode) {
        if (action === 'notify') {
            await openNotifyUsers(promotionId, promoCode);
            return;
        }

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

    notifyUsersList?.addEventListener('change', function (event) {
        if (event.target && event.target.matches('input[type="checkbox"][data-user-id]')) {
            updateSelectedCount();
        }
    });

    notifySelectAllBtn?.addEventListener('click', function () {
        if (!notifyUsersList) return;
        const boxes = notifyUsersList.querySelectorAll('input[type="checkbox"][data-user-id]');
        const select = !notifyAllSelected;
        boxes.forEach(function (box) {
            box.checked = select;
        });
        updateSelectedCount();
    });

    notifySendBtn?.addEventListener('click', sendNotifyEmails);
    notifyCloseBtn?.addEventListener('click', closeNotifyModal);
    notifyCancelBtn?.addEventListener('click', closeNotifyModal);
    notifyModal?.addEventListener('click', function (event) {
        if (event.target === notifyModal) closeNotifyModal();
    });

    generateBtn?.addEventListener('click', generatePromotion);
    refreshBtn?.addEventListener('click', loadPromotions);
    logoutBtn?.addEventListener('click', async function () {
        await fetch('/api/auth/logout', { method: 'POST' });
        window.location.href = '/login';
    });

    loadPromotions();
})();
