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
    sort_order       INT          NOT NULL DEFAULT 0,
    active           BOOLEAN      NOT NULL DEFAULT TRUE,
    UNIQUE (document_type_id, key)
);

-- Business data the agent can combine with document facts (synthetic).
CREATE TABLE patients (
    id         SERIAL PRIMARY KEY,
    mrn        VARCHAR(32)  NOT NULL UNIQUE,
    first_name VARCHAR(100) NOT NULL,
    last_name  VARCHAR(100) NOT NULL,
    birth_date DATE         NOT NULL,
    sex        CHAR(1)      NOT NULL CHECK (sex IN ('F','M','X'))
);

CREATE TABLE lab_orders (
    id         SERIAL PRIMARY KEY,
    patient_id INT         NOT NULL REFERENCES patients(id),
    test_code  VARCHAR(32) NOT NULL,
    ordered_on DATE        NOT NULL,
    status     VARCHAR(16) NOT NULL CHECK (status IN ('ordered','collected','resulted','cancelled'))
);

CREATE TABLE reference_ranges (
    test_code VARCHAR(32)   PRIMARY KEY,
    unit      VARCHAR(32)   NOT NULL,
    low       NUMERIC(10,3),
    high      NUMERIC(10,3)
);

-- Read-only access for this tenant's role (passed in by db/init/01_tenants.sh as :reader).
GRANT USAGE ON SCHEMA public TO :"reader";
GRANT SELECT ON ALL TABLES IN SCHEMA public TO :"reader";
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO :"reader";
