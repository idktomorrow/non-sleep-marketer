-- DuckDB 스키마. 시각은 모두 UTC naive TIMESTAMP로 저장한다 (변환은 nsm/store/db.py).
-- 변경 시 이 파일(새 설치용 최신 형태)을 고치고, nsm/store/db.py의 SCHEMA_VERSION을 올리고
-- MIGRATIONS[새 버전]에 기존 DB를 따라잡게 하는 ALTER 문을 추가한다.

CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS source_items (
    id VARCHAR PRIMARY KEY,
    source VARCHAR NOT NULL,
    url VARCHAR,
    url_hash VARCHAR NOT NULL UNIQUE,
    title VARCHAR NOT NULL,
    text VARCHAR NOT NULL,
    published_at TIMESTAMP,
    collected_at TIMESTAMP NOT NULL,
    relevance DOUBLE,
    topic VARCHAR,
    topic_confidence DOUBLE,
    used_in_content_id VARCHAR
);

CREATE TABLE IF NOT EXISTS classifications (
    id VARCHAR PRIMARY KEY,
    target_type VARCHAR NOT NULL,      -- 'source_item' | 'comment'
    target_id VARCHAR NOT NULL,
    kind VARCHAR NOT NULL,             -- 'relevance' | 'topic' | 'comment_category'
    label VARCHAR NOT NULL,
    confidence DOUBLE NOT NULL,
    classifier VARCHAR NOT NULL,
    created_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS contents (
    id VARCHAR PRIMARY KEY,
    status VARCHAR NOT NULL,
    topic VARCHAR NOT NULL,
    template VARCHAR NOT NULL,
    generated VARCHAR NOT NULL,        -- GeneratedContent JSON
    relevance_confidence DOUBLE NOT NULL,
    is_ad BOOLEAN NOT NULL DEFAULT FALSE,
    review_reason VARCHAR,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    approved_by VARCHAR,               -- 'policy' | 'human'
    persona_topic VARCHAR              -- 페르소나 관심 주제(주제 순환용, v2)
);

CREATE TABLE IF NOT EXISTS publications (
    id VARCHAR PRIMARY KEY,
    content_id VARCHAR NOT NULL,
    platform VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMP,
    external_id VARCHAR,
    url VARCHAR,
    last_error VARCHAR,
    published_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL,
    UNIQUE (content_id, platform)
);

CREATE TABLE IF NOT EXISTS comments (
    id VARCHAR PRIMARY KEY,            -- platform || ':' || external_id
    platform VARCHAR NOT NULL,
    external_id VARCHAR NOT NULL,
    post_external_id VARCHAR NOT NULL,
    author_id VARCHAR NOT NULL,
    author_name VARCHAR NOT NULL,
    text VARCHAR NOT NULL,
    created_at TIMESTAMP NOT NULL,
    fetched_at TIMESTAMP NOT NULL,
    category VARCHAR,
    confidence DOUBLE,
    action VARCHAR,
    status VARCHAR NOT NULL,           -- new | auto_replied | hidden | escalated | ignored | resolved
    reason VARCHAR,
    reply_text VARCHAR,
    reply_external_id VARCHAR,
    handled_at TIMESTAMP,
    handled_by VARCHAR                 -- 'policy' | 'human'
);

CREATE TABLE IF NOT EXISTS reply_counts (
    platform VARCHAR NOT NULL,
    author_id VARCHAR NOT NULL,
    day DATE NOT NULL,
    count INTEGER NOT NULL,
    PRIMARY KEY (platform, author_id, day)
);

CREATE TABLE IF NOT EXISTS metrics_posts (
    platform VARCHAR NOT NULL,
    post_external_id VARCHAR NOT NULL,
    captured_at TIMESTAMP NOT NULL,
    views BIGINT, reach BIGINT, impressions BIGINT,
    likes BIGINT, comments BIGINT, saves BIGINT, shares BIGINT
);

CREATE TABLE IF NOT EXISTS metrics_account (
    platform VARCHAR NOT NULL,
    captured_at TIMESTAMP NOT NULL,
    followers BIGINT
);

CREATE TABLE IF NOT EXISTS costs (
    id VARCHAR PRIMARY KEY,
    kind VARCHAR NOT NULL,
    provider VARCHAR NOT NULL,
    usd DOUBLE NOT NULL,
    occurred_at TIMESTAMP NOT NULL,
    ref VARCHAR
);

CREATE TABLE IF NOT EXISTS run_logs (
    id VARCHAR PRIMARY KEY,
    job VARCHAR NOT NULL,
    started_at TIMESTAMP NOT NULL,
    finished_at TIMESTAMP,
    ok BOOLEAN,
    message VARCHAR
);

CREATE TABLE IF NOT EXISTS settings_kv (
    key VARCHAR PRIMARY KEY,
    value VARCHAR NOT NULL              -- JSON
);

-- 대시보드 알림함(종 모양, N2). 데스크톱 알림을 끄거나 놓쳐도 여기 남는다.
CREATE TABLE IF NOT EXISTS notifications (
    id VARCHAR PRIMARY KEY,
    created_at TIMESTAMP NOT NULL,
    title VARCHAR NOT NULL,
    body VARCHAR NOT NULL,
    level VARCHAR NOT NULL,             -- info / warn / error
    link VARCHAR,                       -- 누르면 갈 대시보드 경로
    read_at TIMESTAMP
);

-- 오늘의 추천(S11 제안, ADR 0051). 콘텐츠 상태(동결)는 그대로 두고, 추천·보관·사진 후보 상태를 옆에 둔다.
CREATE TABLE IF NOT EXISTS picks (
    content_id VARCHAR PRIMARY KEY,
    run_date DATE NOT NULL,             -- 추천한 날(현지)
    rank INTEGER NOT NULL,
    reason VARCHAR NOT NULL,            -- 사람이 읽는 추천 이유
    state VARCHAR NOT NULL,             -- ready | saved | queued | dismissed
    freshness VARCHAR NOT NULL,         -- fresh | stale | unknown (published_at 기준)
    published_at TIMESTAMP,             -- 자료 중 가장 최근 공개 시각
    photo_status VARCHAR NOT NULL,      -- none | candidate | applied | dropped
    photo_candidate_url VARCHAR,
    photo_source_url VARCHAR,
    photo_candidate_path VARCHAR,
    render_checked_at TIMESTAMP,        -- 미리보기 렌더에서 넘침 없음을 확인한 시각
    rechecked_at TIMESTAMP,             -- 보관 글을 게시하기 전 사람이 날짜·가격을 다시 확인한 시각
    title_fingerprint VARCHAR NOT NULL, -- 같은 사건 재추천 방지용(정규화 토큰)
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL
);
