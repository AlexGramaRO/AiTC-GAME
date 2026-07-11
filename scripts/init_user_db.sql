-- AiTC user accounts + subscriptions (PostgreSQL / Railway)
-- Tables are also created automatically on app startup via user_auth.init_db().
-- Run manually in Railway Postgres if you prefer explicit migration:
--   psql "$DATABASE_URL" -f scripts/init_user_db.sql

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    display_name TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected', 'disabled')),
    is_admin BOOLEAN NOT NULL DEFAULT FALSE,
    stripe_customer_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    approved_at TIMESTAMPTZ,
    approved_by UUID REFERENCES users(id) ON DELETE SET NULL,
    rejected_at TIMESTAMPTZ,
    rejected_by UUID REFERENCES users(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    plan_name TEXT NOT NULL DEFAULT 'standard',
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'expired', 'cancelled')),
    notes TEXT,
    stripe_subscription_id TEXT,
    pass_type TEXT NOT NULL DEFAULT 'monthly',
    expires_at TIMESTAMPTZ,
    stripe_checkout_session_id TEXT,
    cancel_at_period_end BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by UUID REFERENCES users(id) ON DELETE SET NULL,
    CHECK (end_date >= start_date)
);

CREATE INDEX IF NOT EXISTS idx_users_email ON users (email);
CREATE INDEX IF NOT EXISTS idx_users_status ON users (status);
CREATE INDEX IF NOT EXISTS idx_subscriptions_user_id ON subscriptions (user_id);
CREATE INDEX IF NOT EXISTS idx_subscriptions_status ON subscriptions (status);
CREATE INDEX IF NOT EXISTS idx_subscriptions_end_date ON subscriptions (end_date);
CREATE UNIQUE INDEX IF NOT EXISTS idx_subscriptions_stripe_subscription_id
    ON subscriptions (stripe_subscription_id)
    WHERE stripe_subscription_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_subscriptions_stripe_checkout_session_id
    ON subscriptions (stripe_checkout_session_id)
    WHERE stripe_checkout_session_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS signup_verifications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    display_name TEXT,
    code_hash TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_signup_verifications_email ON signup_verifications (email);
CREATE INDEX IF NOT EXISTS idx_signup_verifications_expires_at ON signup_verifications (expires_at);

CREATE TABLE IF NOT EXISTS promotion_codes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code TEXT NOT NULL UNIQUE,
    duration_days INTEGER NOT NULL CHECK (duration_days > 0),
    max_uses INTEGER NOT NULL CHECK (max_uses > 0),
    use_count INTEGER NOT NULL DEFAULT 0 CHECK (use_count >= 0),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by UUID REFERENCES users(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS promotion_redemptions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    promotion_id UUID NOT NULL REFERENCES promotion_codes(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    redeemed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (promotion_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_promotion_codes_code ON promotion_codes (code);
CREATE INDEX IF NOT EXISTS idx_promotion_redemptions_user_id ON promotion_redemptions (user_id);
