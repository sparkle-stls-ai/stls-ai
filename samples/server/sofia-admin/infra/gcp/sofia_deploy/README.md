# Sofia Deploy — Infrastruttura Terraform su GCP

Deploy completo dei tre agenti STLS-A2A su **Cloud Run** con:
- **Cloud SQL PostgreSQL 16** (IP privato, nessun IP pubblico)
- **Memorystore Redis 7** (VPC peering diretto)
- **Artifact Registry** (immagini Docker)
- **Secret Manager** (credenziali e API key)
- **VPC dedicato** con Serverless VPC Connector

`BASE_URL` viene risolto automaticamente a runtime da ciascun server tramite
il metadata server GCP — nessuna configurazione manuale necessaria.

---

## Struttura

```
sofia-deploy/                          ← questa repo (infrastruttura)
├── docker/
│   ├── server-api-keys/
│   │   └── Dockerfile                 ← build context: server-api-keys/
│   ├── server-monitoring/
│   │   └── Dockerfile                 ← build context: server-monitoring/
│   └── server-redis/
│       └── Dockerfile                 ← build context: server-redis/
├── scripts/
│   ├── build-push.sh                  ← Build & push delle 3 immagini
│   └── init-db.sql                    ← Schema PostgreSQL iniziale
├── terraform/
│   ├── main.tf                        ← Provider, abilitazione API GCP, locals
│   ├── variables.tf                   ← Tutte le variabili con default e validazioni
│   ├── outputs.tf                     ← Output riepilogo infrastruttura
│   ├── vpc.tf                         ← VPC, subnet, private services access, connector
│   ├── iam.tf                         ← Service account, IAM bindings
│   ├── secrets.tf                     ← Secret Manager (DSN, Redis URL, API key)
│   ├── artifact_registry.tf           ← Registry Docker con cleanup automatico
│   ├── cloudsql.tf                    ← PostgreSQL 16 su IP privato
│   ├── redis.tf                       ← Memorystore Redis 7
│   └── cloudrun.tf                    ← 3 servizi Cloud Run via for_each
├── .gitignore
├── terraform.tfvars.example
└── README.md
```

### Struttura attesa nel repo applicativo

Ogni server ha una propria directory autonoma con `wheels/`, `requirements.txt`
e la cartella `app/` che contiene tutti i sorgenti copiati nel container (in realta' coincidono con quelle docker)

```
<repo-root>/
├── server-api-keys/                   ← build context Dockerfile api-keys
│   ├── wheels/
│   │   └── stls_a2a-X.Y.Z-py3-none-any.whl
│   ├── requirements.txt
│   └── app/                           ← tutto ciò che finisce in /app nel container
│       ├── server.py
│       ├── agent_keys_management/
│       │   └── __init__.py
│       └── ...altri moduli/file
│
├── server-monitoring/                 ← build context Dockerfile monitoring
│   ├── wheels/
│   │   └── stls_a2a-X.Y.Z-py3-none-any.whl
│   ├── requirements.txt
│   └── app/
│       ├── server.py
│       ├── agent_monitoring/
│       └── ...
│
└── server-redis/                      ← build context Dockerfile redis
    ├── wheels/
    │   └── stls_a2a-X.Y.Z-py3-none-any.whl
    ├── requirements.txt
    └── app/
        ├── server.py
        ├── agent_redis/
        └── ...
```

> **Wheel**: ogni server ha la propria copia in `wheels/`. Se preferisci
> un'unica sorgente, puoi usare un symlink o copiarlo con uno script pre-build.

---

## Prerequisiti

```bash
terraform --version   # >= 1.7
gcloud --version
docker --version

gcloud auth login
gcloud auth application-default login
gcloud config set project aichemy-dev-01
```

---

## Deploy — Passo per passo

### 1. Configurazione variabili

```bash
cp terraform.tfvars.example terraform/terraform.tfvars
# Edita terraform.tfvars con i valori reali
```

### 2. Provisioning infrastruttura

```bash
cd terraform
terraform init
terraform plan
terraform apply
```

Al termine l'output mostra IP Redis, IP Cloud SQL e URL Cloud Run.

### 3. Inizializzazione schema PostgreSQL

Connettiti all'IP privato (es. da Cloud Shell nella stessa VPC):

```bash
DB_IP=$(terraform -chdir=terraform output -raw db_private_ip)

psql "host=${DB_IP} dbname=sofia user=spk_sofia password=<PWD>" \
     -f scripts/init-db.sql
```

### 4. Aggiornamento server.py

Copia il `server.py` (e tutti i moduli necessari per l'implementazione del server) nella
cartella `app/` di ogni server nel repo applicativo:


### 5. Build & push immagini Docker

```bash
chmod +x scripts/build-push.sh
./scripts/build-push.sh 1.0.0
```

Lo script per ciascun agente:
- Usa il **Dockerfile specifico** `docker/server-{nome}/Dockerfile`
- Imposta come **build context** la directory del server (`server-{nome}/`)
- Verifica che `wheels/`, `app/` e il wheel stls_a2a siano presenti
- Non copia né sposta file — tutto è nella struttura del repo applicativo

### 6. Deploy servizi

```bash
cd terraform
terraform apply -var='default_image_tag=1.0.0'
```

I servizi si avviano e `BASE_URL` viene risolto automaticamente
dal metadata server GCP — nessun secondo apply necessario.

### 7. Cancellazione servizi via terraform

#### 1. Disabilita la protezione Cloud SQL
cd terraform
terraform apply -var='db_deletion_protection=false'

#### 2. Svuota il registry (obbligatorio se contiene immagini)
gcloud artifacts docker images delete \
  europe-west8-docker.pkg.dev/aichemy-dev-01/sofia/server-api-keys \
  --delete-tags --quiet

gcloud artifacts docker images delete \
  europe-west8-docker.pkg.dev/aichemy-dev-01/sofia/server-monitoring \
  --delete-tags --quiet

gcloud artifacts docker images delete \
  europe-west8-docker.pkg.dev/aichemy-dev-01/sofia/server-redis \
  --delete-tags --quiet

#### 3. Distruggi tutto
terraform destroy

### 8. Sospensione dei servizi

#### 1. Memorystore Redis
terraform destroy -target='google_redis_instance.sofia'

terraform apply -target='google_redis_instance.sofia'

#### 2. CloudSQL
gcloud sql instances patch sofia-pg \
  --activation-policy=NEVER

gcloud sql instances patch sofia-pg \
  --activation-policy=ALWAYS 

#### 3. Riattivazione servizi (occhio al cambio ip redis)

##### 1. Ricrea Redis
terraform apply -target='google_redis_instance.sofia'

##### 2. Aggiorna il secret con il nuovo IP
NEW_IP=$(terraform output -raw redis_host)
echo -n "redis://${NEW_IP}:6379" | \
  gcloud secrets versions add sofia-redis-url --data-file=-

##### 3. Riavvia Cloud SQL
gcloud sql instances patch sofia-pg --activation-policy=ALWAYS

##### 4. Forza il riavvio dei 3 servizi Cloud Run per rileggere il secret
for svc in sofia-api-keys sofia-monitoring sofia-redis; do
  gcloud run services update $svc \
    --region=europe-west8 \
    --update-env-vars _RESTART=$(date +%s)
done

---

## BASE_URL — risoluzione automatica

Ogni `server.py` contiene `_resolve_base_url()` che interroga nell'ordine:

1. Variabile d'ambiente `BASE_URL` (override esplicito, utile in locale)
2. Metadata server GCP `metadata.google.internal` (Cloud Run in produzione)
3. Fallback `http://localhost:{PORT}` (sviluppo locale senza GCP)

L'Agent Card viene generata con l'URL corretto senza configurazione aggiuntiva.

---

## Variabili principali

| Variabile | Default | Descrizione |
|---|---|---|
| `project_id` | — | GCP Project ID |
| `region` | `europe-west8` | Regione GCP (Milan) |
| `db_tier` | `db-g1-small` | Tier Cloud SQL (`db-f1-micro` per dev) |
| `db_availability_type` | `ZONAL` | `ZONAL` o `REGIONAL` (HA) |
| `redis_tier` | `BASIC` | `BASIC` o `STANDARD_HA` |
| `min_instances` | `0` | Scale-to-zero. Usa `1` per eliminare cold start |
| `max_instances` | `5` | Limite istanze per servizio |
| `cloud_run_invoker_public` | `false` | `true` = endpoint pubblici senza auth GCP |
| `default_image_tag` | `latest` | Tag Docker per tutti i servizi |
| `image_tags` | `{}` | Override tag per singolo servizio |

---

## Outputs principali

```bash
terraform -chdir=terraform output summary         # riepilogo completo
terraform -chdir=terraform output service_urls    # URL Cloud Run
terraform -chdir=terraform output db_private_ip   # IP Cloud SQL
terraform -chdir=terraform output redis_host      # IP Redis
```

---

## Aggiornamento servizi

```bash
# Build nuova versione
./scripts/build-push.sh 1.1.0

# Deploy
cd terraform && terraform apply -var='default_image_tag=1.1.0'
```

Per aggiornare un singolo agente:

```hcl
# terraform.tfvars
image_tags = {
  api-keys = "1.1.0"
  # monitoring e redis rimangono sul default_image_tag
}
```

---

## Rotazione secrets

```bash
# Esempio rotazione OPENAI_API_KEY
echo -n "sk-proj-NUOVA_KEY" | \
  gcloud secrets versions add sofia-openai-api-key --data-file=-

# Forza nuovo deploy per rileggere il secret
gcloud run services update sofia-api-keys \
  --region europe-west8 \
  --update-env-vars _FORCE_REDEPLOY=$(date +%s)
```

---

## Note sicurezza

- **PostgreSQL e Redis** senza IP pubblici — raggiungibili solo via VPC privata
- **KEY_PROVIDER_NAME=base** — nessun JVM nel container, immagini leggere
- **Secret Manager** — le credenziali non transitano mai nel Dockerfile
- **`db_deletion_protection = true`** — protezione da cancellazioni accidentali
- **Utente non-root** (`appuser`, UID 1001) in tutti i container
- Imposta `cloud_run_invoker_public = false` per richiedere auth GCP-native

---

## Architettura GCP

```
┌─────────────────────────────────────────────────────────────────────┐
│                     External A2A clients                            │
└───────────────────────────────┬─────────────────────────────────────┘
                                │ HTTPS
       ┌───────────┐            ▼                                ┌───────────────┐
       │ Artifact  │  ┌──────────────────────────────────────┐   │ Secret        │
       │ Registry  │─▶│       Cloud Run — europe-west8      │◀  │ Manager       │
       │ (images)  │  │  ┌──────────┐┌────────────┐┌───────┐ │   │ (secrets)     │
       └───────────┘  │  │ api-keys ││ monitoring ││ redis │ │   └───────────────┘
                      │  └──────────┘└────────────┘└───────┘ │
                      └─────────────────┬────────────────────┘
                                        │
                          ┌─────────────▼───────────────┐
                          │  Serverless VPC Connector   │
                          └──────────────┬──────────────┘
                                         │
        ┌────────────────────────────────▼─────────────────────────────┐
        │                  sofia-network — VPC privata                 │
        │  ┌───────────────────────────┐  ┌──────────────────────────┐ │
        │  │   Memorystore Redis 7     │  │  Cloud SQL PostgreSQL 16 │ │
        │  │  Task store · Auth cache  │  │  sf_api_keys · sf_agents │ │
        │  │  BASIC · DIRECT_PEERING   │  │  ZONAL · IP privato      │ │
        │  └───────────────────────────┘  └──────────────────────────┘ │
        └──────────────────────────────────────────────────────────────┘
```

<div align="center">

<!-- Diagramma architetturale — generato automaticamente -->
<svg width="680" viewBox="0 0 680 480" xmlns="http://www.w3.org/2000/svg" style="max-width:100%;font-family:sans-serif">
<defs>
  <marker id="arr" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
    <path d="M2 1L8 5L2 9" fill="none" stroke="#888780" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
  </marker>
  <marker id="arr-d" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
    <path d="M2 1L8 5L2 9" fill="none" stroke="#B4B2A9" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
  </marker>
</defs>

<!-- External clients -->
<rect x="240" y="20" width="200" height="44" rx="8" fill="#F1EFE8" stroke="#888780" stroke-width="0.5"/>
<text x="340" y="45" text-anchor="middle" font-size="13" font-weight="500" fill="#444441">External A2A clients</text>

<!-- Arrow: External → Cloud Run -->
<line x1="340" y1="64" x2="340" y2="89" stroke="#888780" stroke-width="1.5" fill="none" marker-end="url(#arr)"/>

<!-- Cloud Run container -->
<rect x="120" y="90" width="440" height="130" rx="12" fill="none" stroke="#185FA5" stroke-width="0.5" stroke-dasharray="5 3"/>
<text x="132" y="107" text-anchor="start" font-size="11" fill="#185FA5">Cloud Run — europe-west8</text>

<!-- api-keys -->
<rect x="135" y="115" width="130" height="90" rx="8" fill="#EEEDFE" stroke="#534AB7" stroke-width="0.5"/>
<text x="200" y="148" text-anchor="middle" font-size="13" font-weight="500" fill="#3C3489">api-keys</text>
<text x="200" y="168" text-anchor="middle" font-size="11" fill="#534AB7">API key manager</text>

<!-- monitoring -->
<rect x="280" y="115" width="130" height="90" rx="8" fill="#EEEDFE" stroke="#534AB7" stroke-width="0.5"/>
<text x="345" y="148" text-anchor="middle" font-size="13" font-weight="500" fill="#3C3489">monitoring</text>
<text x="345" y="168" text-anchor="middle" font-size="11" fill="#534AB7">Platform monitor</text>

<!-- redis -->
<rect x="425" y="115" width="130" height="90" rx="8" fill="#EEEDFE" stroke="#534AB7" stroke-width="0.5"/>
<text x="490" y="148" text-anchor="middle" font-size="13" font-weight="500" fill="#3C3489">redis</text>
<text x="490" y="168" text-anchor="middle" font-size="11" fill="#534AB7">Redis interface</text>

<!-- Artifact Registry -->
<rect x="5" y="115" width="105" height="90" rx="8" fill="#F1EFE8" stroke="#888780" stroke-width="0.5"/>
<text x="57" y="147" text-anchor="middle" font-size="13" font-weight="500" fill="#444441">Artifact</text>
<text x="57" y="165" text-anchor="middle" font-size="13" font-weight="500" fill="#444441">Registry</text>
<text x="57" y="183" text-anchor="middle" font-size="11" fill="#5F5E5A">Docker images</text>
<line x1="110" y1="160" x2="119" y2="160" stroke="#B4B2A9" stroke-width="1" stroke-dasharray="3 2" fill="none" marker-end="url(#arr-d)"/>

<!-- Secret Manager -->
<rect x="568" y="115" width="107" height="90" rx="8" fill="#F1EFE8" stroke="#888780" stroke-width="0.5"/>
<text x="621" y="147" text-anchor="middle" font-size="13" font-weight="500" fill="#444441">Secret</text>
<text x="621" y="165" text-anchor="middle" font-size="13" font-weight="500" fill="#444441">Manager</text>
<text x="621" y="183" text-anchor="middle" font-size="11" fill="#5F5E5A">API keys, DSN</text>
<line x1="568" y1="160" x2="561" y2="160" stroke="#B4B2A9" stroke-width="1" stroke-dasharray="3 2" fill="none" marker-end="url(#arr-d)"/>

<!-- Arrow: Cloud Run → VPC Connector -->
<line x1="340" y1="220" x2="340" y2="233" stroke="#888780" stroke-width="1.5" fill="none" marker-end="url(#arr)"/>

<!-- VPC Connector -->
<rect x="200" y="234" width="280" height="36" rx="6" fill="#E1F5EE" stroke="#0F6E56" stroke-width="0.5"/>
<text x="340" y="252" text-anchor="middle" font-size="11" fill="#085041">Serverless VPC Connector</text>

<!-- Arrow: VPC Connector → Private VPC -->
<line x1="340" y1="270" x2="340" y2="279" stroke="#888780" stroke-width="1.5" fill="none" marker-end="url(#arr)"/>

<!-- Private VPC container -->
<rect x="10" y="280" width="660" height="162" rx="16" fill="none" stroke="#0F6E56" stroke-width="0.5" stroke-dasharray="5 3"/>
<text x="22" y="298" text-anchor="start" font-size="11" fill="#0F6E56">sofia-network — VPC privata</text>

<!-- Memorystore Redis -->
<rect x="55" y="305" width="215" height="112" rx="8" fill="#FAEEDA" stroke="#854F0B" stroke-width="0.5"/>
<text x="162" y="340" text-anchor="middle" font-size="13" font-weight="500" fill="#633806">Memorystore Redis 7</text>
<text x="162" y="360" text-anchor="middle" font-size="11" fill="#854F0B">Task store · Auth cache</text>
<text x="162" y="378" text-anchor="middle" font-size="11" fill="#854F0B">BASIC · DIRECT_PEERING</text>

<!-- Cloud SQL PostgreSQL -->
<rect x="410" y="305" width="240" height="112" rx="8" fill="#E6F1FB" stroke="#185FA5" stroke-width="0.5"/>
<text x="530" y="340" text-anchor="middle" font-size="13" font-weight="500" fill="#0C447C">Cloud SQL PostgreSQL 16</text>
<text x="530" y="360" text-anchor="middle" font-size="11" fill="#185FA5">sf_api_keys · sf_agents</text>
<text x="530" y="378" text-anchor="middle" font-size="11" fill="#185FA5">ZONAL · IP privato</text>
</svg>

</div>