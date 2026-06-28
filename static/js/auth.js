(function () {
    'use strict';

    const form = document.getElementById('authForm');
    const modeInput = document.getElementById('authMode');
    const nextInput = document.getElementById('authNext');
    const emailInput = document.getElementById('authEmail');
    const displayNameInput = document.getElementById('authDisplayName');
    const passwordInput = document.getElementById('authPassword');
    const confirmInput = document.getElementById('authConfirmPassword');
    const messageEl = document.getElementById('authMessage');
    const submitBtn = document.getElementById('authSubmitBtn');

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
            body: JSON.stringify(body),
        });
        let data = {};
        try {
            data = await resp.json();
        } catch (_) {
            data = {};
        }
        return { resp, data };
    }

    form?.addEventListener('submit', async function (event) {
        event.preventDefault();
        setMessage('');

        const mode = modeInput?.value || 'login';
        const email = (emailInput?.value || '').trim();
        const password = passwordInput?.value || '';
        const next = (nextInput?.value || '').trim();

        if (!email || !password) {
            setMessage('Email and password are required.', 'error');
            return;
        }

        if (mode === 'signup') {
            const confirm = confirmInput?.value || '';
            if (password.length < 8) {
                setMessage('Password must be at least 8 characters.', 'error');
                return;
            }
            if (password !== confirm) {
                setMessage('Passwords do not match.', 'error');
                return;
            }

            submitBtn.disabled = true;
            try {
                const { resp, data } = await postJson('/api/auth/signup', {
                    email,
                    password,
                    displayName: (displayNameInput?.value || '').trim(),
                });
                if (!resp.ok || !data.ok) {
                    setMessage(data.error || 'Sign-up failed.', 'error');
                    return;
                }
                setMessage(data.message || 'Account created. Awaiting admin approval.', 'success');
                form.reset();
            } catch (_) {
                setMessage('Network error. Try again.', 'error');
            } finally {
                submitBtn.disabled = false;
            }
            return;
        }

        submitBtn.disabled = true;
        try {
            const { resp, data } = await postJson('/api/auth/login', { email, password, next });
            if (!resp.ok || !data.ok) {
                setMessage(data.error || data.message || 'Sign-in failed.', 'error');
                return;
            }

            if (data.user && data.user.status === 'pending') {
                setMessage(data.message || 'Your account is awaiting administrator approval.', 'info');
                return;
            }

            window.location.href = data.redirect || next || '/';
        } catch (_) {
            setMessage('Network error. Try again.', 'error');
        } finally {
            submitBtn.disabled = false;
        }
    });
})();
