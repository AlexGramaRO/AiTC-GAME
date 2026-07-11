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

    const verifyModal = document.getElementById('signupVerifyModal');
    const verifyEmailLabel = document.getElementById('signupVerifyEmailLabel');
    const verifyTimerEl = document.getElementById('signupVerifyTimer');
    const verifyCodeInput = document.getElementById('signupVerifyCode');
    const verifyMessageEl = document.getElementById('signupVerifyMessage');
    const verifySubmitBtn = document.getElementById('signupVerifySubmitBtn');
    const verifyChangeEmailBtn = document.getElementById('signupVerifyChangeEmailBtn');

    let verifyTimerId = null;
    let verifyExpiresAtMs = 0;
    let pendingVerificationId = '';
    let pendingSignupEmail = '';

    function setMessage(text, kind) {
        if (!messageEl) return;
        messageEl.textContent = text || '';
        messageEl.style.display = text ? 'block' : 'none';
        messageEl.className = 'auth-message' + (kind ? ' auth-message-' + kind : '');
    }

    function setVerifyMessage(text, kind) {
        if (!verifyMessageEl) return;
        verifyMessageEl.textContent = text || '';
        verifyMessageEl.style.display = text ? 'block' : 'none';
        verifyMessageEl.className = 'auth-message' + (kind ? ' auth-message-' + kind : '');
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

    function stopVerifyTimer() {
        if (verifyTimerId) {
            clearInterval(verifyTimerId);
            verifyTimerId = null;
        }
    }

    function formatRemaining(ms) {
        const totalSeconds = Math.max(0, Math.ceil(ms / 1000));
        const minutes = Math.floor(totalSeconds / 60);
        const seconds = totalSeconds % 60;
        return `${minutes}:${String(seconds).padStart(2, '0')}`;
    }

    function updateVerifyTimerDisplay() {
        const remaining = verifyExpiresAtMs - Date.now();
        if (verifyTimerEl) verifyTimerEl.textContent = formatRemaining(remaining);
        if (remaining <= 0) {
            stopVerifyTimer();
            setVerifyMessage('Verification code expired. Close this window and sign up again.', 'error');
            if (verifySubmitBtn) verifySubmitBtn.disabled = true;
        }
    }

    function openVerifyModal(email, verificationId, expiresAt) {
        pendingVerificationId = verificationId || '';
        pendingSignupEmail = email || '';
        if (verifyEmailLabel) verifyEmailLabel.textContent = email || 'your email';
        if (verifyCodeInput) {
            verifyCodeInput.value = '';
            verifyCodeInput.disabled = false;
        }
        if (verifySubmitBtn) verifySubmitBtn.disabled = false;
        setVerifyMessage('');
        verifyExpiresAtMs = expiresAt ? Date.parse(expiresAt) : (Date.now() + (5 * 60 * 1000));
        stopVerifyTimer();
        updateVerifyTimerDisplay();
        verifyTimerId = setInterval(updateVerifyTimerDisplay, 1000);
        if (verifyModal) {
            verifyModal.style.display = 'flex';
            verifyModal.setAttribute('aria-hidden', 'false');
        }
        setTimeout(() => verifyCodeInput?.focus(), 50);
    }

    function closeVerifyModal() {
        stopVerifyTimer();
        pendingVerificationId = '';
        pendingSignupEmail = '';
        if (verifyModal) {
            verifyModal.style.display = 'none';
            verifyModal.setAttribute('aria-hidden', 'true');
        }
        setVerifyMessage('');
        if (verifyCodeInput) verifyCodeInput.value = '';
    }

    verifyChangeEmailBtn?.addEventListener('click', function () {
        closeVerifyModal();
        setMessage('Update your email address and click Create account again.', 'info');
        emailInput?.focus();
    });

    verifySubmitBtn?.addEventListener('click', async function () {
        const code = (verifyCodeInput?.value || '').trim();
        if (!/^\d{6}$/.test(code)) {
            setVerifyMessage('Enter the 6-digit code from your email.', 'error');
            return;
        }
        if (!pendingVerificationId) {
            setVerifyMessage('Verification session expired. Please sign up again.', 'error');
            return;
        }
        if (Date.now() >= verifyExpiresAtMs) {
            setVerifyMessage('Verification code expired. Please sign up again.', 'error');
            return;
        }

        verifySubmitBtn.disabled = true;
        setVerifyMessage('');
        try {
            const { resp, data } = await postJson('/api/auth/signup/verify', {
                verificationId: pendingVerificationId,
                code,
            });
            if (!resp.ok || !data.ok) {
                setVerifyMessage(data.error || 'Verification failed.', 'error');
                verifySubmitBtn.disabled = false;
                return;
            }
            closeVerifyModal();
            form?.reset();
            setMessage(
                data.message || 'Account created. Your account is awaiting administrator approval — please allow up to 24 hours.',
                'success'
            );
        } catch (_) {
            setVerifyMessage('Network error. Try again.', 'error');
            verifySubmitBtn.disabled = false;
        }
    });

    verifyCodeInput?.addEventListener('keydown', function (event) {
        if (event.key === 'Enter') {
            event.preventDefault();
            verifySubmitBtn?.click();
        }
    });

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
                const { resp, data } = await postJson('/api/auth/signup/start', {
                    email,
                    password,
                    displayName: (displayNameInput?.value || '').trim(),
                });
                if (!resp.ok || !data.ok) {
                    setMessage(data.error || 'Could not start sign-up verification.', 'error');
                    return;
                }
                openVerifyModal(data.email || email, data.verificationId, data.expiresAt);
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
