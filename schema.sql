PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS Profile (
    ProfileName     TEXT PRIMARY KEY,
    AuthType        TEXT NOT NULL DEFAULT 'none',
    SsoStartUrl     TEXT NOT NULL DEFAULT '',
    SsoRegion       TEXT NOT NULL DEFAULT '',
    AccountId       TEXT NOT NULL DEFAULT '',
    RoleName        TEXT NOT NULL DEFAULT '',
    DefaultRegion   TEXT NOT NULL DEFAULT 'us-east-1',
    OutputFormat    TEXT NOT NULL DEFAULT 'json',
    HasCredentials  INTEGER NOT NULL DEFAULT 0,
    IsEnabled       INTEGER NOT NULL DEFAULT 1,
    CreatedAtUtc    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS Target (
    TargetId         TEXT PRIMARY KEY,
    ProfileName      TEXT NOT NULL,
    DisplayName      TEXT NOT NULL,
    TargetType       TEXT NOT NULL,
    AwsTarget        TEXT NOT NULL,
    RemoteHost       TEXT NULL,
    RemotePort       INTEGER NOT NULL,
    LocalPort        INTEGER NOT NULL,
    Env              TEXT NULL,
    Region           TEXT NULL,
    GroupTitle       TEXT NULL,
    Description      TEXT NULL,
    IsEnabled        INTEGER NOT NULL DEFAULT 1,
    SortOrder        INTEGER NOT NULL DEFAULT 0,
    CreatedAtUtc     TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(ProfileName) REFERENCES Profile(ProfileName) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS IX_Target_ProfileName ON Target(ProfileName);
CREATE INDEX IF NOT EXISTS IX_Target_Env ON Target(Env);
CREATE INDEX IF NOT EXISTS IX_Target_GroupTitle ON Target(GroupTitle);

-- ✅ De-dup guard (prevents duplicate Targets for same natural identity)
-- NOTE: SQLite does NOT support "ON CONFLICT ..." in CREATE INDEX reliably.
CREATE UNIQUE INDEX IF NOT EXISTS UX_Target_Natural
ON Target(
  ProfileName,
  AwsTarget,
  IFNULL(RemoteHost,''),
  RemotePort,
  IFNULL(Region,''),
  IFNULL(GroupTitle,'')
);

-- Optional future table (NOT required because we persist ports in JSON or deterministic hashing)
CREATE TABLE IF NOT EXISTS PortAssignment (
    MapKey        TEXT PRIMARY KEY,
    LocalPort     INTEGER NOT NULL,
    CreatedAtUtc  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS UX_PortAssignment_LocalPort ON PortAssignment(LocalPort);

CREATE TABLE IF NOT EXISTS JumpboxPreference (
    ProfileName TEXT NOT NULL,
    Region      TEXT NOT NULL,
    Env         TEXT NOT NULL,
    TargetType  TEXT NOT NULL,
    JumpboxId   TEXT NOT NULL,
    UpdatedAtUtc TEXT NOT NULL,
    PRIMARY KEY (ProfileName, Region, Env, TargetType)
);