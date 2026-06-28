(function () {
    'use strict';

    const tableBody = document.getElementById('adminUsersTableBody');
    const statusFilter = document.getElementById('adminUsersStatusFilter');
    const refreshBtn = document.getElementById('adminUsersRefreshBtn');
    const logoutBtn = document.getElementById('adminUsersLogoutBtn');
    const messageEl = document.getElementById('adminUsersMessage');

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

    function subscriptionSummary(user) {
        const parts = [];
        if (user.activeMonthlySubscription) {
            const m = user.activeMonthlySubscription;
            parts.push(m.planName + ' · ' + formatDate(m.startDate) + ' → ' + formatDate(m.endDate) + ' (monthly)');
        }
        if (user.activeOneDayPass) {
            const d = user.activeOneDayPass;
            parts.push(d.planName + ' · until ' + formatDateTime(d.expiresAt || d.endDate) + ' (24h pass)');
        }
        if (parts.length) return parts.join(' · ');
        const subs = user.subscriptions || [];
        if (!subs.length) return 'None';
        const latest = subs[0];
        return latest.planName + ' · ' + formatDate(latest.startDate) + ' → ' + formatDate(latest.endDate) + ' (' + latest.status + ')';
    }

    function actionButton(action, userId, label, primary) {
        const cls = primary ? 'btn btn-primary admin-user-action' : 'btn btn-secondary admin-user-action';
        return '<button type="button" class="' + cls + '" data-action="' + action + '" data-id="' + escapeHtml(userId) + '">' + escapeHtml(label) + '</button>';
    }

    function actionButtons(user) {
        const id = user.id;
        const parts = [];

        if (user.isAdmin) {
            return '<span class="hint-text">Admin account</span>';
        }

        if (user.status === 'pending') {
            parts.push(actionButton('approve', id, 'Approve', true));
            parts.push(actionButton('reject', id, 'Reject', false));
        } else if (user.status === 'rejected') {
            parts.push(actionButton('approve', id, 'Approve', true));
            parts.push(actionButton('disable', id, 'Disable', false));
        } else if (user.status === 'disabled') {
            parts.push(actionButton('approve', id, 'Move to approved', true));
        } else if (user.status === 'approved') {
            parts.push(actionButton('disable', id, 'Disable', false));
            parts.push(actionButton('subscription', id, 'Add 31-day sub', false));
            parts.push(actionButton('one-day-pass', id, 'Add 24h pass', false));
            if (user.activeMonthlySubscription) {
                parts.push(actionButton('cancel-subscription', id, 'Cancel subscription', false));
            }
            if (user.activeOneDayPass) {
                parts.push(actionButton('cancel-one-day-pass', id, 'Cancel One Day Pass', false));
            }
            if (user.activeMonthlySubscription && user.activeOneDayPass) {
                parts.push(actionButton('revoke-active', id, 'Cancel all access', false));
            }
        }

        if (!parts.length) return '<span class="hint-text">—</span>';
        return '<div class="admin-user-actions">' + parts.join('') + '</div>';
    }

    function renderUsers(users) {
        if (!tableBody) return;
        if (!users.length) {
            tableBody.innerHTML = '<tr><td colspan="6" class="hint-text">No users found.</td></tr>';
            return;
        }
        tableBody.innerHTML = users.map(function (user) {
            return '<tr>' +
                '<td>' + escapeHtml(user.email) + (user.isAdmin ? ' <span class="admin-badge">Admin</span>' : '') + '</td>' +
                '<td>' + escapeHtml(user.displayName || '—') + '</td>' +
                '<td><span class="status-pill status-' + escapeHtml(user.status) + '">' + escapeHtml(user.status) + '</span></td>' +
                '<td>' + escapeHtml(subscriptionSummary(user)) + '</td>' +
                '<td>' + escapeHtml(formatDate(user.createdAt)) + '</td>' +
                '<td>' + actionButtons(user) + '</td>' +
                '</tr>';
        }).join('');
    }

    function escapeHtml(text) {
        return String(text || '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    async function loadUsers() {
        setMessage('');
        const status = statusFilter?.value || '';
        const qs = status ? ('?status=' + encodeURIComponent(status)) : '';
        try {
            const resp = await fetch('/api/admin/user-accounts' + qs);
            const data = await resp.json();
            if (!resp.ok || !data.ok) {
                setMessage(data.error || 'Failed to load users.', 'error');
                return;
            }
            renderUsers(data.users || []);
        } catch (_) {
            setMessage('Network error while loading users.', 'error');
        }
    }

    async function runAction(action, userId) {
        let url = '/api/admin/user-accounts/' + encodeURIComponent(userId) + '/' + action;
        let body = undefined;

        if (action === 'subscription') {
            url = '/api/admin/user-accounts/' + encodeURIComponent(userId) + '/subscriptions';
            body = JSON.stringify({ planName: 'standard' });
        } else if (action === 'one-day-pass') {
            url = '/api/admin/user-accounts/' + encodeURIComponent(userId) + '/one-day-pass';
            body = JSON.stringify({ planName: 'admin-one-day-pass' });
        } else if (action === 'cancel-subscription') {
            url = '/api/admin/user-accounts/' + encodeURIComponent(userId) + '/cancel-subscription';
        } else if (action === 'cancel-one-day-pass') {
            url = '/api/admin/user-accounts/' + encodeURIComponent(userId) + '/cancel-one-day-pass';
        } else if (action === 'revoke-active') {
            url = '/api/admin/user-accounts/' + encodeURIComponent(userId) + '/subscriptions/revoke-active';
        }

        const resp = await fetch(url, {
            method: 'POST',
            headers: body ? { 'Content-Type': 'application/json' } : undefined,
            body,
        });
        const data = await resp.json();
        if (!resp.ok || !data.ok) {
            throw new Error(data.error || 'Action failed');
        }
        return data;
    }

    function confirmAction(action) {
        if (action === 'cancel-subscription') {
            return window.confirm('Cancel this user\'s active monthly subscription? They will lose subscription-based access immediately.');
        }
        if (action === 'cancel-one-day-pass') {
            return window.confirm('Cancel this user\'s active One Day Pass? They will lose pass-based access immediately.');
        }
        if (action === 'revoke-active') {
            return window.confirm('Cancel all active subscriptions and passes for this user?');
        }
        return true;
    }

    tableBody?.addEventListener('click', async function (event) {
        const btn = event.target.closest('.admin-user-action');
        if (!btn) return;
        const action = btn.getAttribute('data-action');
        const userId = btn.getAttribute('data-id');
        if (!action || !userId) return;

        if (!confirmAction(action)) {
            return;
        }

        btn.disabled = true;
        try {
            await runAction(action, userId);
            setMessage('Updated successfully.', 'success');
            await loadUsers();
        } catch (err) {
            setMessage(err.message || 'Action failed.', 'error');
        } finally {
            btn.disabled = false;
        }
    });

    refreshBtn?.addEventListener('click', loadUsers);
    statusFilter?.addEventListener('change', loadUsers);

    logoutBtn?.addEventListener('click', async function () {
        await fetch('/api/auth/logout', { method: 'POST' });
        window.location.href = '/login';
    });

    loadUsers();
})();
