-- Tenant "acme": a lab network. Synthetic data only.
INSERT INTO document_types (code, name, description) VALUES
  ('lab_report', 'Lab report', 'Laboratory results report for one patient and one collection date');

INSERT INTO field_definitions
  (document_type_id, key, label, description, data_type, required, multiple, allowed_values, unit, join_target, sort_order)
SELECT d.id, f.key, f.label, f.description, f.data_type, f.required, f.multiple,
       f.allowed_values::jsonb, f.unit, f.join_target, f.sort_order
FROM document_types d,
(VALUES
  ('patient_mrn',  'MRN',              'Medical record number of the patient, usually in the header', 'string',  TRUE,  FALSE, NULL, NULL, 'patients.mrn', 1),
  ('collected_on', 'Collection date',  'Date the sample was collected',                               'date',    TRUE,  FALSE, NULL, NULL, NULL, 2),
  ('hemoglobin',   'Hemoglobin',       'Hemoglobin (Hb, HGB) result',                                 'decimal', FALSE, FALSE, NULL, 'g/dL', NULL, 3),
  ('glucose',      'Glucose',          'Fasting glucose result',                                      'decimal', FALSE, FALSE, NULL, 'mg/dL', NULL, 4),
  ('hiv_result',   'HIV screening',    'HIV 1/2 antigen/antibody screening result',                   'enum',    FALSE, FALSE, '["positive","negative"]', NULL, NULL, 5)
) AS f(key, label, description, data_type, required, multiple, allowed_values, unit, join_target, sort_order)
WHERE d.code = 'lab_report';

INSERT INTO patients (mrn, first_name, last_name, birth_date, sex) VALUES
  ('00123', 'Ana',   'Testa',    '1985-03-14', 'F'),
  ('00124', 'Bruno', 'Ficticio', '1972-11-02', 'M'),
  ('00125', 'Carla', 'Ejemplo',  '1990-07-21', 'F');

INSERT INTO lab_orders (patient_id, test_code, ordered_on, status)
SELECT p.id, o.test_code, o.ordered_on::date, o.status
FROM patients p
JOIN (VALUES
  ('00123', 'HGB', '2026-11-01', 'resulted'),
  ('00123', 'GLU', '2026-11-01', 'resulted'),
  ('00124', 'HGB', '2026-11-10', 'collected'),
  ('00125', 'HIV', '2026-11-12', 'ordered')
) AS o(mrn, test_code, ordered_on, status) ON o.mrn = p.mrn;

INSERT INTO reference_ranges (test_code, unit, low, high) VALUES
  ('HGB', 'g/dL',  12.0, 15.5),
  ('GLU', 'mg/dL', 70,   99);
