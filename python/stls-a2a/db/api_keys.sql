-- ── Dati di test ────────────────────────────────────────────────────────────
-- ATTENZIONE: sostituire i key_id con hash reali generati con generate_api_key()
-- I valori qui sotto sono placeholder e non funzioneranno come chiavi valide.

INSERT INTO sf_api_keys (key_id, client_id, scopes, endpoints, description, metadata)
VALUES
    -- Chiave per lc-agent (tutti gli endpoint lc)
    (
        'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
        'lc-test-client',
        ARRAY['secure-a2a'],
        ARRAY['/agents/lc-agent/secure-a2a', '/agents/lc-agent/a2a'],
        'Chiave di test lc-agent — sostituire con hash reale',
        '{"tenant": "test", "env": "development"}'::jsonb
    ),
    -- Chiave admin con accesso globale
    (
        'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        'admin-postgres',
        ARRAY['secure-a2a', 'admin'],
        ARRAY[]::TEXT[],        -- wildcard
        'Chiave admin Postgres — sostituire con hash reale',
        '{"role": "admin", "env": "development"}'::jsonb
    ),
    -- Chiave scaduta (per test rifiuto)
    (
        'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
        'expired-pg-client',
        ARRAY['secure-a2a'],
        ARRAY['/agents/lc-agent/secure-a2a'],
        'Test: chiave scaduta — deve restituire 401',
        '{}'::jsonb
    )
ON CONFLICT (key_id) DO NOTHING;

-- Imposta la scadenza sulla chiave scaduta
UPDATE sf_api_keys
   SET expires_at = '2020-01-01T00:00:00Z'
 WHERE key_id = 'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc';

-- ── Helper: rivoca una chiave ───────────────────────────────────────────────
-- UPDATE sf_api_keys SET revoked = true WHERE key_id = '<sha256>';

-- ── Helper: lista chiavi attive ─────────────────────────────────────────────
-- SELECT key_id, client_id, scopes, expires_at, description
--   FROM sf_api_keys
--  WHERE revoked = false
--  ORDER BY created_at DESC;

-- ── Helper: elimina chiavi scadute da più di 90 giorni ─────────────────────
-- DELETE FROM sf_api_keys
--  WHERE revoked = true
--    AND created_at < now() - INTERVAL '90 days';