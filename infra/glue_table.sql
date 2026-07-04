-- Athena external table over the S3 data lake.
-- Upload studies data as Parquet (or CSV) to s3://<bucket>/studies/ first.
CREATE DATABASE IF NOT EXISTS suitcase;

CREATE EXTERNAL TABLE IF NOT EXISTS suitcase.studies (
  study_id        STRING,
  study_title     STRING,
  compound        STRING,
  species         STRING,
  route           STRING,
  dose_mg_kg      DOUBLE,
  duration_days   INT,
  findings_summary STRING
)
STORED AS PARQUET
LOCATION 's3://your-data-lake-bucket/studies/'
TBLPROPERTIES ('parquet.compression'='SNAPPY');
