-- Only runs on a new volume. psql reads passwords from the container environment.
\getenv app_password APP_DB_PASSWORD
\getenv control_password CONTROL_DB_PASSWORD
CREATE ROLE pa_app LOGIN PASSWORD :'app_password' NOSUPERUSER NOBYPASSRLS;
CREATE ROLE pa_control LOGIN PASSWORD :'control_password' NOSUPERUSER NOBYPASSRLS;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
