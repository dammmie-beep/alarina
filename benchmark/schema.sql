CREATE TABLE IF NOT EXISTS repos (
    id           INTEGER PRIMARY KEY,
    full_name    TEXT    NOT NULL UNIQUE,
    language     TEXT,
    stars        INTEGER,
    collected_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS issue_pr_pairs (
    id                           INTEGER PRIMARY KEY,
    repo_id                      INTEGER NOT NULL REFERENCES repos(id),
    issue_number                 INTEGER NOT NULL,
    issue_title                  TEXT    NOT NULL,
    issue_body                   TEXT,
    issue_labels                 TEXT,           -- JSON array of label names
    issue_created_at             TEXT,
    issue_closed_at              TEXT,
    pr_number                    INTEGER NOT NULL,
    pr_merged_at                 TEXT,
    lines_added                  INTEGER,
    lines_deleted                INTEGER,
    files_changed_count          INTEGER,
    files_changed_paths          TEXT,           -- JSON array of file paths
    review_rounds                INTEGER,
    first_contributor_comment_at TEXT,           -- NULL if no comments before merge
    time_to_merge_hours          REAL,           -- NULL if first_contributor_comment_at is NULL
    fixer_login                  TEXT,
    fixer_prior_contributions    INTEGER,
    UNIQUE(repo_id, issue_number, pr_number)
);

CREATE INDEX IF NOT EXISTS idx_pairs_repo ON issue_pr_pairs(repo_id);
