-- Read-only login used by the application (never the admin user).
-- Local development password only; in Azure use Entra ID auth / Key Vault.
CREATE ROLE chatmed_reader LOGIN PASSWORD 'reader_local_only';
