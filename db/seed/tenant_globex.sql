-- Tenant "globex": sponsor running study GLX-202 (synthetic). Smaller CIOMS field set than acme,
-- to show that extraction follows each tenant's own metadata.
INSERT INTO document_types (code, name, description) VALUES
  ('cioms', 'CIOMS I form', 'CIOMS I suspected adverse reaction report for one subject and one case');

INSERT INTO field_definitions
  (document_type_id, key, label, description, data_type, required, multiple, allowed_values, unit, join_target, sort_order)
SELECT d.id, f.key, f.label, f.description, f.data_type, f.required, f.multiple,
       f.allowed_values::jsonb, f.unit, f.join_target, f.sort_order
FROM document_types d,
(VALUES
  ('subject_id',          'Subject ID',         'Clinical study subject identifier (SUBJID, e.g. 20101-007)', 'string', TRUE,  FALSE, NULL, NULL, 'adsl.subjid', 1),
  ('reaction_onset_date', 'Reaction onset',     'Boxes 4-6: date the reaction started', 'date', TRUE, FALSE, NULL, NULL, NULL, 2),
  ('reaction_description','Reaction description','Boxes 7+13: description of the reaction(s)', 'string', TRUE, FALSE, NULL, NULL, NULL, 3),
  ('serious',             'Serious',            'Any of boxes 8-12 (died, hospitalisation, disability, life threatening) is checked', 'boolean', FALSE, FALSE, NULL, NULL, NULL, 4),
  ('suspect_drug',        'Suspect drug',       'Box 14: suspect drug name(s), one per entry', 'string', TRUE, TRUE, NULL, NULL, NULL, 5),
  ('report_type',         'Report type',        'Box 25a: initial or follow-up report', 'enum', FALSE, FALSE, '["initial","followup"]', NULL, NULL, 6)
) AS f(key, label, description, data_type, required, multiple, allowed_values, unit, join_target, sort_order)
WHERE d.code = 'cioms';

INSERT INTO adsl (studyid, usubjid, subjid, siteid, age, sex, country, arm, trt01a, trtsdt, trtedt, saffl) VALUES
  ('GLX-202', 'GLX-202-20101-007', '20101-007', '20101', 72, 'M', 'USA', 'Velorin 5 mg', 'Velorin 5 mg', '2026-02-15', '2026-07-31', 'Y'),
  ('GLX-202', 'GLX-202-20102-002', '20102-002', '20102', 58, 'F', 'CAN', 'Velorin 5 mg', 'Velorin 5 mg', '2026-03-01', NULL,         'Y');

INSERT INTO adae (studyid, usubjid, aeseq, aeterm, aedecod, aebodsys, astdt, aendt, aesev, aeser, aerel, aeacn, aeout, trtemfl) VALUES
  ('GLX-202', 'GLX-202-20101-007', 1, 'fainting episode', 'Syncope', 'Nervous system disorders', '2026-05-09', '2026-05-09', 'SEVERE', 'Y', 'POSSIBLY RELATED', 'DRUG INTERRUPTED', 'RECOVERED/RESOLVED', 'Y'),
  ('GLX-202', 'GLX-202-20102-002', 1, 'fatigue',          'Fatigue', 'General disorders and administration site conditions', '2026-04-20', NULL, 'MILD', 'N', 'NOT RELATED', 'DOSE NOT CHANGED', 'NOT RECOVERED/NOT RESOLVED', 'Y');
