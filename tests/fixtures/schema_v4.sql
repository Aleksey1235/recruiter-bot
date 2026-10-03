
CREATE TABLE IF NOT EXISTS users (
    discord_id INTEGER PRIMARY KEY,
    username TEXT,
    static_id TEXT,
    role TEXT DEFAULT 'recruiter',
    level INTEGER DEFAULT 1,
    total_salary REAL NOT NULL DEFAULT 0,
    paid_salary REAL NOT NULL DEFAULT 0,
    warns INTEGER NOT NULL DEFAULT 0,
    notes TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (level >= 1),
    CHECK (warns >= 0)
);

CREATE TABLE IF NOT EXISTS shifts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    creator_id INTEGER,
    scheduled_start TIMESTAMP NOT NULL,
    scheduled_end TIMESTAMP NOT NULL,
    slots INTEGER NOT NULL DEFAULT 1,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    message_id INTEGER,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (slots >= 0),
    CHECK (scheduled_end > scheduled_start),
    CHECK (status IN ('open', 'booked', 'active', 'completed', 'cancelled', 'missed'))
);

CREATE TABLE IF NOT EXISTS shift_members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shift_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    static_id TEXT,
    status TEXT NOT NULL DEFAULT 'booked',
    actual_start TIMESTAMP,
    actual_end TIMESTAMP,
    cancel_reason TEXT,
    report_id INTEGER,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(shift_id, user_id),
    FOREIGN KEY (shift_id) REFERENCES shifts(id) ON DELETE CASCADE,
    CHECK (status IN ('booked', 'active', 'completed', 'cancelled', 'removed', 'missed'))
);

CREATE TABLE IF NOT EXISTS shift_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shift_id INTEGER NOT NULL,
    member_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    total_accepted INTEGER NOT NULL DEFAULT 0,
    came_to_base INTEGER NOT NULL DEFAULT 0,
    found_by_recruiter INTEGER NOT NULL DEFAULT 0,
    comment TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    reviewed_by INTEGER,
    reviewed_at TIMESTAMP,
    reject_reason TEXT,
    message_id INTEGER,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(member_id),
    FOREIGN KEY (shift_id) REFERENCES shifts(id) ON DELETE CASCADE,
    FOREIGN KEY (member_id) REFERENCES shift_members(id) ON DELETE CASCADE,
    CHECK (total_accepted >= 0),
    CHECK (came_to_base >= 0),
    CHECK (found_by_recruiter >= 0),
    CHECK (came_to_base + found_by_recruiter <= total_accepted),
    CHECK (status IN ('pending', 'approved', 'rejected'))
);

CREATE TABLE IF NOT EXISTS invites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    static_id TEXT NOT NULL UNIQUE,
    invited_by INTEGER NOT NULL,
    full_name TEXT,
    ticket TEXT,
    last_name_changed TEXT,
    organization TEXT,
    fraction TEXT,
    info TEXT,
    notes TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    reviewed_by INTEGER,
    reviewed_at TIMESTAMP,
    reject_reason TEXT,
    message_id INTEGER,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (status IN ('pending', 'accepted', 'rejected'))
);

CREATE TABLE IF NOT EXISTS goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    type TEXT NOT NULL,
    target_value INTEGER NOT NULL DEFAULT 0,
    current_value INTEGER NOT NULL DEFAULT 0,
    period TEXT NOT NULL DEFAULT 'неделя',
    status TEXT NOT NULL DEFAULT 'active',
    created_by INTEGER,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (type IN ('люди', 'смены', 'часы')),
    CHECK (period IN ('день', 'неделя', 'месяц')),
    CHECK (status IN ('active', 'deleted')),
    CHECK (target_value >= 0),
    CHECK (current_value >= 0)
);

CREATE TABLE IF NOT EXISTS finances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    amount REAL NOT NULL,
    type TEXT NOT NULL,
    reason TEXT,
    status TEXT NOT NULL DEFAULT 'accrued',
    created_by INTEGER,
    related_shift_id INTEGER,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (amount > 0),
    CHECK (type IN ('salary', 'pay')),
    CHECK (status IN ('accrued', 'paid')),
    CHECK ((type='salary' AND status='accrued') OR (type='pay' AND status='paid'))
);

CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    action TEXT NOT NULL,
    object_type TEXT,
    object_id INTEGER,
    details TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL DEFAULT 0,
    type TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, type, object_type, object_id),
    CHECK (status IN ('pending', 'sent', 'failed')),
    CHECK (attempts >= 0)
);

CREATE TABLE IF NOT EXISTS blacklist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id INTEGER NOT NULL,
    discord_tag TEXT NOT NULL,
    static_id TEXT,
    full_name TEXT,
    reason TEXT NOT NULL,
    evidence TEXT,
    notes TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_by INTEGER NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    removed_by INTEGER,
    removed_at TIMESTAMP,
    remove_reason TEXT,
    CHECK (discord_id > 0),
    CHECK (status IN ('active', 'removed'))
);



CREATE INDEX IF NOT EXISTS idx_shifts_start_status
    ON shifts(scheduled_start, status);
CREATE INDEX IF NOT EXISTS idx_shift_members_user_status
    ON shift_members(user_id, status);
CREATE INDEX IF NOT EXISTS idx_shift_members_shift_status
    ON shift_members(shift_id, status);
CREATE INDEX IF NOT EXISTS idx_shift_members_actual_start
    ON shift_members(actual_start);
CREATE INDEX IF NOT EXISTS idx_shift_reports_user_status
    ON shift_reports(user_id, status);
CREATE INDEX IF NOT EXISTS idx_shift_reports_created
    ON shift_reports(created_at);
CREATE INDEX IF NOT EXISTS idx_finances_user_type
    ON finances(user_id, type);
CREATE INDEX IF NOT EXISTS idx_goals_user_status
    ON goals(user_id, status);
CREATE INDEX IF NOT EXISTS idx_invites_status_created
    ON invites(status, created_at);
CREATE INDEX IF NOT EXISTS idx_logs_created
    ON logs(created_at);
CREATE INDEX IF NOT EXISTS idx_notifications_status
    ON notifications(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_blacklist_status_created
    ON blacklist(status, created_at);
CREATE INDEX IF NOT EXISTS idx_blacklist_tag
    ON blacklist(discord_tag);
CREATE UNIQUE INDEX IF NOT EXISTS ux_blacklist_active_discord
    ON blacklist(discord_id) WHERE status='active';
CREATE UNIQUE INDEX IF NOT EXISTS ux_blacklist_active_static
    ON blacklist(static_id) WHERE status='active' AND static_id IS NOT NULL AND TRIM(static_id)<>'';

PRAGMA user_version=4;
