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
            const label = m.displayPlanName || 'Monthly subscription';
            parts.push(label + ' · ' + formatDate(m.startDate) + ' → ' + formatDate(m.endDate) + ' (monthly)');
        }
        if (user.activeOneDayPass) {
            const d = user.activeOneDayPass;
            const label = d.displayPlanName || d.planName || 'One Day Pass';
            parts.push(label + ' · until ' + formatDateTime(d.expiresAt || d.endDate) + ' (24h pass)');
        }
        if (user.activePromoAccess) {
            const p = user.activePromoAccess;
            const label = p.displayPlanName || 'Promo access';
            parts.push(label + ' · until ' + formatDateTime(p.expiresAt || p.endDate) + ' (promo)');
        }
        if (parts.length) return parts.join(' · ');
        const subs = user.subscriptions || [];
        if (!subs.length) return 'None';
        const latest = subs[0];
        return latest.planName + ' · ' + formatDate(latest.startDate) + ' → ' + formatDate(latest.endDate) + ' (' + latest.status + ')';
    }

    function escapeHtml(text) {
        return String(text || '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    function buildActionItems(user) {
        const id = user.id;
        const items = [];

        if (user.isAdmin) {
            return items;
        }

        if (user.status === 'pending') {
            items.push({ action: 'approve', label: 'Approve', primary: true });
            items.push({ action: 'reject', label: 'Reject' });
        } else if (user.status === 'rejected') {
            items.push({ action: 'approve', label: 'Approve', primary: true });
            items.push({ action: 'disable', label: 'Disable' });
        } else if (user.status === 'disabled') {
            items.push({ action: 'approve', label: 'Move to approved', primary: true });
        } else if (user.status === 'approved') {
            items.push({ action: 'disable', label: 'Disable' });
            items.push({ action: 'subscription', label: 'Add 31-day sub' });
            items.push({ action: 'one-day-pass', label: 'Add 24h pass' });
            if (user.activeMonthlySubscription) {
                items.push({ action: 'cancel-subscription', label: 'Cancel subscription' });
            }
            if (user.activeOneDayPass) {
                items.push({ action: 'cancel-one-day-pass', label: 'Cancel One Day Pass' });
            }
            if (user.activePromoAccess) {
                items.push({ action: 'cancel-promo-access', label: 'Cancel promo access' });
            }
            const activeAccessCount = [
                user.activeMonthlySubscription,
                user.activeOneDayPass,
                user.activePromoAccess,
            ].filter(Boolean).length;
            if (activeAccessCount >= 2) {
                items.push({ action: 'revoke-active', label: 'Cancel all access' });
            }
        }

        if (items.length) {
            items.push({ type: 'divider' });
        }
        items.push({ action: 'delete', label: 'Delete user', danger: true });
        return items;
    }

    function actionMenuItem(item, userId) {
        if (item.type === 'divider') {
            return '<div class="admin-user-actions-divider" role="separator"></div>';
        }
        let cls = 'admin-user-action admin-user-action-item';
        if (item.primary) cls += ' admin-user-action-primary';
        if (item.danger) cls += ' admin-user-action-danger';
        return '<button type="button" class="' + cls + '" role="menuitem" data-action="' + item.action + '" data-id="' + escapeHtml(userId) + '">' + escapeHtml(item.label) + '</button>';
    }

    function actionButtons(user) {
        if (user.isAdmin) {
            return '<span class="hint-text">Admin account</span>';
        }

        const items = buildActionItems(user);
        if (!items.length) {
            return '<span class="hint-text">—</span>';
        }

        const menuHtml = items.map(function (item) {
            return actionMenuItem(item, user.id);
        }).join('');

        return '<div class="admin-user-actions-menu">' +
            '<button type="button" class="btn btn-secondary admin-user-actions-trigger" aria-haspopup="true" aria-expanded="false">Actions</button>' +
            '<div class="admin-user-actions-dropdown" role="menu">' + menuHtml + '</div>' +
            '</div>';
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
                '<td class="admin-users-actions-cell">' + actionButtons(user) + '</td>' +
                '</tr>';
        }).join('');
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
        let method = 'POST';
        let body = undefined;

        if (action === 'subscription') {
            url = '/api/admin/user-accounts/' + encodeURIComponent(userId) + '/subscriptions';
            body = JSON.stringify({ planName: 'monthly-subscription' });
        } else if (action === 'one-day-pass') {
            url = '/api/admin/user-accounts/' + encodeURIComponent(userId) + '/one-day-pass';
            body = JSON.stringify({ planName: 'admin-one-day-pass' });
        } else if (action === 'cancel-subscription') {
            url = '/api/admin/user-accounts/' + encodeURIComponent(userId) + '/cancel-subscription';
        } else if (action === 'cancel-one-day-pass') {
            url = '/api/admin/user-accounts/' + encodeURIComponent(userId) + '/cancel-one-day-pass';
        } else if (action === 'cancel-promo-access') {
            url = '/api/admin/user-accounts/' + encodeURIComponent(userId) + '/cancel-promo-access';
        } else if (action === 'revoke-active') {
            url = '/api/admin/user-accounts/' + encodeURIComponent(userId) + '/subscriptions/revoke-active';
        } else if (action === 'delete') {
            url = '/api/admin/user-accounts/' + encodeURIComponent(userId);
            method = 'DELETE';
        }

        const resp = await fetch(url, {
            method: method,
            headers: body ? { 'Content-Type': 'application/json' } : undefined,
            body: body,
        });
        const data = await resp.json();
        if (!resp.ok || !data.ok) {
            throw new Error(data.error || 'Action failed');
        }
        return data;
    }

    function confirmAction(action, btn) {
        const emailCell = btn?.closest('tr')?.querySelector('td');
        const emailLabel = emailCell ? emailCell.textContent.trim() : 'this user';

        if (action === 'delete') {
            return window.confirm(
                'Permanently delete ' + emailLabel + '?\n\n'
                + 'This removes the account, subscriptions, and promo history. This cannot be undone.'
            );
        }
        if (action === 'cancel-subscription') {
            return window.confirm('Cancel this user\'s active monthly subscription? They will lose subscription-based access immediately.');
        }
        if (action === 'cancel-one-day-pass') {
            return window.confirm('Cancel this user\'s active One Day Pass? They will lose pass-based access immediately.');
        }
        if (action === 'cancel-promo-access') {
            return window.confirm('Cancel this user\'s active promo access? They will lose promo-based access immediately.');
        }
        if (action === 'revoke-active') {
            return window.confirm('Cancel all active subscriptions, passes, and promo access for this user?');
        }
        return true;
    }

    tableBody?.addEventListener('click', async function (event) {
        const btn = event.target.closest('.admin-user-action');
        if (!btn) return;
        const action = btn.getAttribute('data-action');
        const userId = btn.getAttribute('data-id');
        if (!action || !userId) return;

        if (!confirmAction(action, btn)) {
            return;
        }

        btn.disabled = true;
        try {
            await runAction(action, userId);
            setMessage(action === 'delete' ? 'User deleted.' : 'Updated successfully.', 'success');
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
