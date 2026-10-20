"""
Configurazione della sessione di test.

Carica .env automaticamente per i run locali, coerentemente con le
altre parti dell'ecosistema STLS AI. Nessuna variabile viene sovrascritta
se già esportata a livello di shell/CI (override=False, default di
load_dotenv) — così un `export FOO=bar` esplicito prima di `pytest`
continua a vincere su quanto scritto in .env.

Se .env non esiste (es. in CI, dove le variabili arrivano dai secret
del pipeline), load_dotenv() è un no-op silenzioso: nessun errore.
"""

from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")