# entra in psql come superuser
psql -U postgres

# dentro psql:
CREATE DATABASE sofia;
CREATE USER spk_sofia WITH PASSWORD 'CHANGE_ME_STRONG_PASSWORD';
GRANT ALL PRIVILEGES ON DATABASE sofia TO spk_sofia;

psql -U postgres -d sofia

GRANT ALL ON SCHEMA public TO spk_sofia;
ALTER DATABASE sofia OWNER TO spk_sofia;