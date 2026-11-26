-- Tenant "acme": sponsor running study ACM-101 (synthetic). All subjects, events and drugs are invented.
INSERT INTO document_types (code, name, description) VALUES
  ('cioms', 'CIOMS I form', 'CIOMS I suspected adverse reaction report for one subject and one case');

-- Field metadata follows the public CIOMS I form sections. Patient initials and date of birth are
-- deliberately NOT extracted (data minimisation): the subject id is enough to join to ADSL.
INSERT INTO field_definitions
  (document_type_id, key, label, description, data_type, required, multiple, allowed_values, unit, join_target, sort_order)
SELECT d.id, f.key, f.label, f.description, f.data_type, f.required, f.multiple,
       f.allowed_values::jsonb, f.unit, f.join_target, f.sort_order
FROM document_types d,
(VALUES
  ('subject_id',          'Subject ID',               'Clinical study subject identifier (SUBJID, e.g. 01001-004); usually near the patient section or in the narrative', 'string', TRUE,  FALSE, NULL, NULL, 'adsl.subjid', 1),
  ('country',             'Country',                  'Box 1a: country where the reaction occurred, as written', 'string', FALSE, FALSE, NULL, NULL, NULL, 2),
  ('age',                 'Age',                      'Box 2a: age of the patient in years', 'integer', FALSE, FALSE, NULL, 'years', NULL, 3),
  ('sex',                 'Sex',                      'Box 3: sex of the patient (F = female, M = male)', 'enum', FALSE, FALSE, '["F","M","U"]', NULL, NULL, 4),
  ('reaction_onset_date', 'Reaction onset',           'Boxes 4-6: date the reaction started', 'date', TRUE,  FALSE, NULL, NULL, NULL, 5),
  ('reaction_description','Reaction description',     'Boxes 7+13: description of the reaction(s), including relevant tests and lab data', 'string', TRUE, FALSE, NULL, NULL, NULL, 6),
  ('patient_died',        'Patient died',             'Boxes 8-12: seriousness criterion "patient died" is checked', 'boolean', FALSE, FALSE, NULL, NULL, NULL, 7),
  ('hospitalisation',     'Hospitalisation',          'Boxes 8-12: "involved or prolonged inpatient hospitalisation" is checked', 'boolean', FALSE, FALSE, NULL, NULL, NULL, 8),
  ('disability',          'Disability or incapacity', 'Boxes 8-12: "involved persistent or significant disability or incapacity" is checked', 'boolean', FALSE, FALSE, NULL, NULL, NULL, 9),
  ('life_threatening',    'Life threatening',         'Boxes 8-12: "life threatening" is checked', 'boolean', FALSE, FALSE, NULL, NULL, NULL, 10),
  ('suspect_drug',        'Suspect drug',             'Box 14: suspect drug name(s), one per entry', 'string', TRUE, TRUE, NULL, NULL, NULL, 11),
  ('daily_dose',          'Daily dose',               'Box 15: daily dose of the suspect drug, as written', 'string', FALSE, FALSE, NULL, NULL, NULL, 12),
  ('route',               'Route',                    'Box 16: route of administration', 'string', FALSE, FALSE, NULL, NULL, NULL, 13),
  ('indication',          'Indication',               'Box 17: indication for use', 'string', FALSE, FALSE, NULL, NULL, NULL, 14),
  ('therapy_start_date',  'Therapy start',            'Box 18: start date of therapy with the suspect drug', 'date', FALSE, FALSE, NULL, NULL, NULL, 15),
  ('dechallenge',         'Abated after stopping',    'Box 20: did the reaction abate after stopping the drug?', 'enum', FALSE, FALSE, '["yes","no","na"]', NULL, NULL, 16),
  ('rechallenge',         'Reappeared after reintroduction', 'Box 21: did the reaction reappear after reintroduction?', 'enum', FALSE, FALSE, '["yes","no","na"]', NULL, NULL, 17),
  ('concomitant_drugs',   'Concomitant drugs',        'Box 22: concomitant drugs, one per entry (exclude those used to treat the reaction)', 'string', FALSE, TRUE, NULL, NULL, NULL, 18),
  ('mfr_control_no',      'Manufacturer control no.', 'Box 24b: manufacturer control number of the case', 'string', FALSE, FALSE, NULL, NULL, NULL, 19),
  ('date_received',       'Date received',            'Box 24c: date received by manufacturer', 'date', FALSE, FALSE, NULL, NULL, NULL, 20),
  ('report_type',         'Report type',              'Box 25a: initial or follow-up report', 'enum', FALSE, FALSE, '["initial","followup"]', NULL, NULL, 21)
) AS f(key, label, description, data_type, required, multiple, allowed_values, unit, join_target, sort_order)
WHERE d.code = 'cioms';

-- Fields whose value is a tick mark on the form: OCR text alone can't tell if they're checked (D13).
UPDATE field_definitions SET checkbox = TRUE
WHERE key IN ('patient_died','hospitalisation','disability','life_threatening',
              'dechallenge','rechallenge','report_type');

INSERT INTO adsl (studyid, usubjid, subjid, siteid, age, sex, country, arm, trt01a, trtsdt, trtedt, saffl) VALUES
  ('ACM-101', 'ACM-101-01001-001', '01001-001', '01001', 54, 'F', 'ARG', 'Zentrobal 20 mg', 'Zentrobal 20 mg', '2026-03-02', '2026-08-30', 'Y'),
  ('ACM-101', 'ACM-101-01001-002', '01001-002', '01001', 61, 'M', 'ARG', 'Placebo',         'Placebo',         '2026-03-05', '2026-09-01', 'Y'),
  ('ACM-101', 'ACM-101-01001-004', '01001-004', '01001', 47, 'F', 'ARG', 'Zentrobal 20 mg', 'Zentrobal 20 mg', '2026-03-10', '2026-06-14', 'Y'),
  ('ACM-101', 'ACM-101-01002-001', '01002-001', '01002', 68, 'M', 'ESP', 'Zentrobal 20 mg', 'Zentrobal 20 mg', '2026-04-01', '2026-09-28', 'Y'),
  ('ACM-101', 'ACM-101-01002-003', '01002-003', '01002', 39, 'F', 'ESP', 'Placebo',         'Placebo',         '2026-04-07', NULL,         'N');

-- Subject 01001-004 has the serious event described by the synthetic CIOMS fixture (onset 2026-06-12,
-- hospitalisation, drug withdrawn). Keep these values in sync with scripts/make_fixtures.py.
INSERT INTO adae (studyid, usubjid, aeseq, aeterm, aedecod, aebodsys, astdt, aendt, aesev, aeser, aerel, aeacn, aeout, trtemfl) VALUES
  ('ACM-101', 'ACM-101-01001-001', 1, 'headache',               'Headache',             'Nervous system disorders',                        '2026-03-20', '2026-03-22', 'MILD',     'N', 'POSSIBLY RELATED', 'DOSE NOT CHANGED', 'RECOVERED/RESOLVED',           'Y'),
  ('ACM-101', 'ACM-101-01001-001', 2, 'nausea',                 'Nausea',               'Gastrointestinal disorders',                      '2026-05-02', '2026-05-09', 'MILD',     'N', 'POSSIBLY RELATED', 'DOSE NOT CHANGED', 'RECOVERED/RESOLVED',           'Y'),
  ('ACM-101', 'ACM-101-01001-002', 1, 'back pain',              'Back pain',            'Musculoskeletal and connective tissue disorders', '2026-04-11', NULL,         'MODERATE', 'N', 'NOT RELATED',      'DOSE NOT CHANGED', 'NOT RECOVERED/NOT RESOLVED',   'Y'),
  ('ACM-101', 'ACM-101-01001-004', 1, 'rash on arms',           'Rash',                 'Skin and subcutaneous tissue disorders',          '2026-04-03', '2026-04-15', 'MILD',     'N', 'PROBABLY RELATED', 'DOSE NOT CHANGED', 'RECOVERED/RESOLVED',           'Y'),
  ('ACM-101', 'ACM-101-01001-004', 2, 'acute liver injury',     'Drug-induced liver injury', 'Hepatobiliary disorders',                    '2026-06-12', '2026-07-20', 'SEVERE',   'Y', 'PROBABLY RELATED', 'DRUG WITHDRAWN',   'RECOVERED/RESOLVED',           'Y'),
  ('ACM-101', 'ACM-101-01002-001', 1, 'dizziness',              'Dizziness',            'Nervous system disorders',                        '2026-05-18', '2026-05-19', 'MILD',     'N', 'NOT RELATED',      'DOSE NOT CHANGED', 'RECOVERED/RESOLVED',           'Y'),
  ('ACM-101', 'ACM-101-01002-001', 2, 'atrial fibrillation',    'Atrial fibrillation',  'Cardiac disorders',                               '2026-08-07', NULL,         'SEVERE',   'Y', 'POSSIBLY RELATED', 'DOSE REDUCED',     'RECOVERING/RESOLVING',         'Y');
