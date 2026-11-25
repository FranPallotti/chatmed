-- Tenant metadata: describes WHAT can be extracted from each document type.
-- It never holds values taken from patient documents.
-- (Postgres syntax; adapt types for Azure SQL if needed.)

CREATE TABLE document_types (
    id              SERIAL PRIMARY KEY,
    code            VARCHAR(64)  NOT NULL UNIQUE,   -- e.g. 'lab_report'
    name            VARCHAR(200) NOT NULL,
    description     TEXT         NOT NULL,          -- helps the model, also shown to users
    active          BOOLEAN      NOT NULL DEFAULT TRUE
);

CREATE TABLE field_definitions (
    id                SERIAL PRIMARY KEY,
    document_type_id  INT          NOT NULL REFERENCES document_types(id),
    key               VARCHAR(64)  NOT NULL,        -- machine name: 'hemoglobin'
    label             VARCHAR(200) NOT NULL,        -- 'Hemoglobin'
    description       TEXT         NOT NULL,        -- what it is / where it usually appears
    data_type         VARCHAR(16)  NOT NULL
        CHECK (data_type IN ('string','integer','decimal','date','boolean','enum')),
    required          BOOLEAN      NOT NULL DEFAULT FALSE,
    multiple          BOOLEAN      NOT NULL DEFAULT FALSE,  -- can appear several times
    allowed_values    JSONB,                         -- for enum: ["positive","negative"]
    unit              VARCHAR(32),                   -- 'g/dL'
    join_target       VARCHAR(128),                  -- e.g. 'patients.mrn': links doc to DB rows
    sort_order        INT          NOT NULL DEFAULT 0,
    active            BOOLEAN      NOT NULL DEFAULT TRUE,
    UNIQUE (document_type_id, key)
);

-- Query used to build the extraction schema for one document type.
-- ORDER BY key keeps the generated schema (and its hash) stable.
-- SELECT f.key, f.label, f.description, f.data_type, f.required, f.multiple,
--        f.allowed_values, f.unit, f.join_target
-- FROM field_definitions f
-- JOIN document_types d ON d.id = f.document_type_id
-- WHERE d.code = %(doc_type)s AND d.active AND f.active
-- ORDER BY f.key;
