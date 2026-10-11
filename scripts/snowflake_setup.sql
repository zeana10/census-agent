-- One-time Snowflake setup for the census agent. Run as ACCOUNTADMIN in a worksheet,
-- after getting the "US Open Census Data & Neighborhood Insights" listing from the Marketplace.
-- Run ALL statements: select everything (Cmd/Ctrl+A), then click Run. The Run button
-- on its own executes only the statement under the cursor. Safe to re-run.
--
-- Creates a least-privilege role + user the app connects with:
--   * read-only access to the census share (IMPORTED PRIVILEGES), nothing else
--   * an X-Small warehouse that auto-suspends, with a credit cap
--   * a statement timeout as a backstop to the app-side timeout

USE ROLE ACCOUNTADMIN;

-- The share's database name below is what Snowsight shows under Data > Databases; edit it if yours differs.

CREATE WAREHOUSE IF NOT EXISTS CENSUS_WH
  WAREHOUSE_SIZE = 'XSMALL' AUTO_SUSPEND = 60 AUTO_RESUME = TRUE INITIALLY_SUSPENDED = TRUE
  STATEMENT_TIMEOUT_IN_SECONDS = 30;

CREATE RESOURCE MONITOR IF NOT EXISTS CENSUS_RM WITH CREDIT_QUOTA = 10
  TRIGGERS ON 80 PERCENT DO NOTIFY ON 100 PERCENT DO SUSPEND;
ALTER WAREHOUSE CENSUS_WH SET RESOURCE_MONITOR = CENSUS_RM;

CREATE ROLE IF NOT EXISTS CENSUS_READER;
GRANT USAGE ON WAREHOUSE CENSUS_WH TO ROLE CENSUS_READER;
GRANT IMPORTED PRIVILEGES ON DATABASE US_OPEN_CENSUS_DATA_NEIGHBORHOOD_INSIGHTS_FREE_DATASET TO ROLE CENSUS_READER;

-- Service user with key-pair auth (no password). Generate the key locally:
--   openssl genrsa 2048 | openssl pkcs8 -topk8 -inform PEM -out rsa_key.p8 -nocrypt
--   openssl rsa -in rsa_key.p8 -pubout -out rsa_key.pub
-- and paste the body of rsa_key.pub (without the BEGIN/END lines) below.
CREATE USER IF NOT EXISTS CENSUS_APP
  TYPE = SERVICE
  DEFAULT_ROLE = CENSUS_READER
  DEFAULT_WAREHOUSE = CENSUS_WH
  RSA_PUBLIC_KEY = '<paste public key body here>';
-- IF NOT EXISTS skips the whole CREATE when the user already exists, so set the key
-- explicitly as well. That makes the script safe to re-run after a partial run.
ALTER USER CENSUS_APP SET RSA_PUBLIC_KEY = '<paste public key body here>';
GRANT ROLE CENSUS_READER TO USER CENSUS_APP;
-- Put the role under SYSADMIN (standard practice) so admins, including you, can USE ROLE it for checks.
GRANT ROLE CENSUS_READER TO ROLE SYSADMIN;

-- Sanity check, run as the app's role: proves it can see the share. Any number above 0 means it worked.
USE ROLE CENSUS_READER;
USE WAREHOUSE CENSUS_WH;
SELECT COUNT(*) AS tables_visible_to_app
FROM US_OPEN_CENSUS_DATA_NEIGHBORHOOD_INSIGHTS_FREE_DATASET.INFORMATION_SCHEMA.TABLES
WHERE TABLE_SCHEMA = 'PUBLIC';
