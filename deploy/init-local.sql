-- LOCAL ONLY. Production roles/passwords are provisioned in a secret manager.
CREATE ROLE pa_app LOGIN PASSWORD 'local-app-only' NOSUPERUSER NOBYPASSRLS;
CREATE ROLE pa_control LOGIN PASSWORD 'local-control-only' NOSUPERUSER NOBYPASSRLS;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
CREATE DATABASE portfolio_test;
\connect portfolio_test
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
