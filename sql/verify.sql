-- Replace the expected PRJ-* value before running these checks.

SELECT * FROM it_dev.sample_project.car_makes ORDER BY make_id;
SELECT * FROM it_dev.sample_project.car_models ORDER BY model_id;

SELECT table_name, tag_name, tag_value
FROM it_dev.information_schema.table_tags
WHERE schema_name = 'sample_project'
  AND table_name IN ('car_makes', 'car_models')
ORDER BY table_name, tag_name;

SELECT * FROM it_prod.sample_project.car_makes ORDER BY make_id;
SELECT * FROM it_prod.sample_project.car_models ORDER BY model_id;

SELECT table_name, tag_name, tag_value
FROM it_prod.information_schema.table_tags
WHERE schema_name = 'sample_project'
  AND table_name IN ('car_makes', 'car_models')
ORDER BY table_name, tag_name;
