-- Schema shared by every tenant database.
-- Metadata tables describe WHAT can be extracted from documents; they never hold document values.

CREATE TABLE document_types (
    id          SERIAL PRIMARY KEY,
    code        VARCHAR(64)  NOT NULL UNIQUE,
    name        VARCHAR(200) NOT NULL,
    description TEXT         NOT NULL,
    active      BOOLEAN      NOT NULL DEFAULT TRUE
);

CREATE TABLE field_definitions (
    id               SERIAL PRIMARY KEY,
    document_type_id INT          NOT NULL REFERENCES document_types(id),
    key              VARCHAR(64)  NOT NULL,
    label            VARCHAR(200) NOT NULL,
    description      TEXT         NOT NULL,
    data_type        VARCHAR(16)  NOT NULL
        CHECK (data_type IN ('string','integer','decimal','date','boolean','enum')),
    required         BOOLEAN      NOT NULL DEFAULT FALSE,
    multiple         BOOLEAN      NOT NULL DEFAULT FALSE,
    allowed_values   JSONB,
    unit             VARCHAR(32),
    join_target      VARCHAR(128),
    checkbox         BOOLEAN      NOT NULL DEFAULT FALSE,  -- value is a tick mark on the form (D13)
    sort_order       INT          NOT NULL DEFAULT 0,
    active           BOOLEAN      NOT NULL DEFAULT TRUE,
    UNIQUE (document_type_id, key)
);

-- Clinical trial data (synthetic), modeled on a subset of the public CDISC ADaM standard.
-- ADSL: one row per subject. ADAE: one row per adverse event. Lowercase ADaM variable names.
CREATE TABLE adsl (
    studyid  VARCHAR(20)  NOT NULL,
    usubjid  VARCHAR(40)  PRIMARY KEY,           -- unique subject id across studies
    subjid   VARCHAR(20)  NOT NULL UNIQUE,       -- subject id within the study (appears on CIOMS)
    siteid   VARCHAR(10)  NOT NULL,
    age      INT          NOT NULL,
    ageu     VARCHAR(10)  NOT NULL DEFAULT 'YEARS',
    sex      CHAR(1)      NOT NULL CHECK (sex IN ('F','M','U')),
    country  CHAR(3)      NOT NULL,              -- ISO 3166 alpha-3
    arm      VARCHAR(60)  NOT NULL,              -- planned arm
    trt01a   VARCHAR(60)  NOT NULL,              -- actual treatment
    trtsdt   DATE,                               -- first dose date
    trtedt   DATE,                               -- last dose date
    saffl    CHAR(1)      NOT NULL CHECK (saffl IN ('Y','N'))   -- safety population flag
);

CREATE TABLE adae (
    id        SERIAL       PRIMARY KEY,
    studyid   VARCHAR(20)  NOT NULL,
    usubjid   VARCHAR(40)  NOT NULL REFERENCES adsl(usubjid),
    aeseq     INT          NOT NULL,             -- sequence number within the subject
    aeterm    VARCHAR(200) NOT NULL,             -- reported term (verbatim)
    aedecod   VARCHAR(200) NOT NULL,             -- dictionary preferred term
    aebodsys  VARCHAR(200) NOT NULL,             -- system organ class
    astdt     DATE         NOT NULL,             -- start date
    aendt     DATE,                              -- end date (NULL = ongoing)
    aesev     VARCHAR(10)  NOT NULL CHECK (aesev IN ('MILD','MODERATE','SEVERE')),
    aeser     CHAR(1)      NOT NULL CHECK (aeser IN ('Y','N')),          -- serious
    aerel     VARCHAR(20)  NOT NULL,             -- relationship to study drug
    aeacn     VARCHAR(30)  NOT NULL,             -- action taken with study drug
    aeout     VARCHAR(40)  NOT NULL,             -- outcome
    trtemfl   CHAR(1)      NOT NULL CHECK (trtemfl IN ('Y','N')),        -- treatment-emergent
    UNIQUE (usubjid, aeseq)
);

-- Read-only access for this tenant's role (passed in by db/init/01_tenants.sh as :reader).
GRANT USAGE ON SCHEMA public TO :"reader";
GRANT SELECT ON ALL TABLES IN SCHEMA public TO :"reader";
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO :"reader";
