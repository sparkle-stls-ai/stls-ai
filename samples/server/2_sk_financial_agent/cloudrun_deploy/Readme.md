# Deploy layout — before running docker build / cloudbuild

```
deploy/
├── Dockerfile
├── cloudbuild.yaml
├── deploy.sh
├── requirements.txt
├── .dockerignore
│
├── app/                          ← your Python source
│   ├── server.py
│   ├── semantic_agents/
│   └── ...
│
├── wheels/
│   └── stls_a2a-0.1.0-py3-none-any.whl
│
└── jars/
    └── stls-ai-0.0.1.jar         ← copy from build/libs/
```

## Preparazione rapida

```bash
# 1. Copia il JAR dal progetto Java
cp ~/VSProjects/stls-ai-java-sdk/stls-ai/build/libs/stls-ai-0.0.1.jar jars/

# 2. Copia il wheel STLS
cp /path/to/stls_a2a-0.1.0-py3-none-any.whl wheels/

# 3. Build locale (test)
docker build -t stls-a2a-local .
docker run --rm -p 8080:8080 stls-a2a-local

# 4. GCP
## Autenticazione
gcloud auth login --no-launch-browser
## Imposta il progetto
gcloud config set project TUO_PROJECT_ID

## Abilita le API necessarie
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com

## Crea il repo su Artifact Registry (una volta sola)
gcloud artifacts repositories create stls-images \
  --repository-format=docker \
  --location=europe-west8

## Autorizza Cloud Build a deployare su Cloud Run
PROJECT_NUMBER=$(gcloud projects describe TUO_PROJECT_ID --format="value(projectNumber)")
gcloud projects add-iam-policy-binding TUO_PROJECT_ID \
  --member="serviceAccount:${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com" \
  --role="roles/run.admin"
gcloud projects add-iam-policy-binding TUO_PROJECT_ID \
  --member="serviceAccount:${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com" \
  --role="roles/iam.serviceAccountUser"

## Creo il secret OPENAI_API_KEY
gcloud services enable secretmanager.googleapis.com --project=aichemy-dev-01

echo -n "sk-..." | gcloud secrets create OPENAI_API_KEY \
  --data-file=- \
  --project=aichemy-dev-01  
### per visualizzarlo
gcloud secrets versions access latest \
  --secret=OPENAI_API_KEY \
  --project=aichemy-dev-01 | cat  

## Abilito cloudrun a leggere il secret
gcloud secrets add-iam-policy-binding OPENAI_API_KEY \
  --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor" \
  --project=aichemy-dev-01  

## 4. Deploy via Cloud Build
gcloud builds submit \
  --config=cloudbuild.yaml \
  --project=TUO_PROJECT_ID

### 4.1 per vedere errori durante la build
gcloud beta builds log c2ebb295-5963-4858-8003-a954fa90c4fe \
  --project=aichemy-dev-01 2>&1 | grep -A 30 "Step #0"  

gcloud logging read \
  'resource.type="cloud_run_revision" AND resource.labels.revision_name="stls-a2a-sk-agent-00002-kmz"' \
  --project=aichemy-dev-01 \
  --limit=50 \
  --format="value(textPayload)"


### L'url generato sara' del tipo
https://stls-a2a-sk-agent-xxxxxxxxxx-ew.a.run.app

### Restart del container - puo' servire ad esempio a seguito di modifica del secret
gcloud run services update stls-a2a-sk-agent \
  --region=europe-west8 \
  --project=aichemy-dev-01 \
  --update-env-vars=RESTART=$(date +%s)


## 5. visibilita' pubblica
gcloud beta run services add-iam-policy-binding stls-a2a-sk-agent \
  --region=europe-west8 \
  --member=allUsers \
  --role=roles/run.invoker \
  --project=aichemy-dev-01

gcloud builds submit \
  --config=cloudbuild.yaml \
  --substitutions=_REGION=europe-west8,_SERVICE=stls-a2a-sk-agent,_AR_REPO=stls-images,_BASE_URL=https://<your-url>
```


## CLASSPATH nel container

Il JAR viene copiato in `/opt/stls/lib/stls-ai-0.0.1.jar` e la variabile
`CLASSPATH` è impostata staticamente nel Dockerfile.  
Non è necessario alcun Secret Manager entry per CLASSPATH.

Se in futuro servono JAR aggiuntivi basta aggiungere nel Dockerfile:

```dockerfile
COPY jars/altro.jar /opt/stls/lib/altro.jar
ENV CLASSPATH=/opt/stls/lib/stls-ai-0.0.1.jar:/opt/stls/lib/altro.jar
```