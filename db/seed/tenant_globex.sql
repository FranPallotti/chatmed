-- Tenant "globex": a hospital. Different document type and fields than acme. Synthetic data only.
INSERT INTO document_types (code, name, description) VALUES
  ('discharge_summary', 'Discharge summary', 'Summary written when a patient leaves the hospital');

INSERT INTO field_definitions
  (document_type_id, key, label, description, data_type, required, multiple, allowed_values, unit, join_target, sort_order)
SELECT d.id, f.key, f.label, f.description, f.data_type, f.required, f.multiple,
       f.allowed_values::jsonb, f.unit, f.join_target, f.sort_order
FROM document_types d,
(VALUES
  ('patient_mrn',       'MRN',               'Medical record number of the patient',        'string', TRUE,  FALSE, NULL, NULL, 'patients.mrn', 1),
  ('admission_date',    'Admission date',    'Date the patient was admitted',               'date',   TRUE,  FALSE, NULL, NULL, NULL, 2),
  ('discharge_date',    'Discharge date',    'Date the patient was discharged',             'date',   TRUE,  FALSE, NULL, NULL, NULL, 3),
  ('primary_diagnosis', 'Primary diagnosis', 'Main diagnosis as written in the summary',    'string', FALSE, FALSE, NULL, NULL, NULL, 4),
  ('medications',       'Discharge medications', 'Each medication prescribed at discharge', 'string', FALSE, TRUE,  NULL, NULL, NULL, 5),
  ('readmission_risk',  'Readmission risk',  'Readmission risk level stated in the summary','enum',   FALSE, FALSE, '["low","medium","high"]', NULL, NULL, 6)
) AS f(key, label, description, data_type, required, multiple, allowed_values, unit, join_target, sort_order)
WHERE d.code = 'discharge_summary';

INSERT INTO patients (mrn, first_name, last_name, birth_date, sex) VALUES
  ('G-9001', 'Diego', 'Prueba', '1960-01-30', 'M'),
  ('G-9002', 'Elena', 'Muestra', '1978-05-09', 'F');

INSERT INTO lab_orders (patient_id, test_code, ordered_on, status)
SELECT p.id, 'HGB', '2026-10-20', 'resulted' FROM patients p WHERE p.mrn = 'G-9001';

INSERT INTO reference_ranges (test_code, unit, low, high) VALUES
  ('HGB', 'g/dL', 13.5, 17.5);
