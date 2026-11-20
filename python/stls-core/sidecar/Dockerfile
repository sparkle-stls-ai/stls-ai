# python/stls-core/sidecar/Dockerfile (issue #38)
# ------------------------------------------------------------------------
# arqit_sidecar.py / arqit_device_agent.py as a container image, local-
# development only -- the Arqit SKA-Platform(tm) wheels baked in here are
# proprietary and NOT redistributable (see python/stls-core/README.md).
# This image is built and `minikube image load`ed locally; it must never
# be pushed to a registry.
#
# Build context MUST be the repo root (needs python/stls-core, including
# its local arqit_wheels/):
#
#   docker build -f python/stls-core/sidecar/Dockerfile -t naas18-sidecar:dev .
#   minikube image load naas18-sidecar:dev
#
# Which script runs (arqit_sidecar.py vs arqit_device_agent.py) and with
# which flags is picked by the Pod spec's own `command`/`args` (see
# samples/peer/18_naas_federation/k8s/*.yaml).
FROM python:3.12-slim
WORKDIR /build

COPY python/stls-core ./python/stls-core

RUN pip install --upgrade pip \
 && pip install -e "./python/stls-core[redis]" \
 && pip install -e "./python/stls-core[arqit_py]" \
      --find-links ./python/stls-core/arqit_wheels/arqit-skaplatform-common/ \
      --find-links ./python/stls-core/arqit_wheels/arqit-skaplatform-policy/ \
      --find-links ./python/stls-core/arqit_wheels/arqit-skaplatform-registration/ \
      --find-links ./python/stls-core/arqit_wheels/arqit-skaplatform-provisioning/ \
      --find-links ./python/stls-core/arqit_wheels/arqit-skaplatform-mqtt-common/ \
      --find-links ./python/stls-core/arqit_wheels/arqit-skaplatform-peering/ \
      --find-links ./python/stls-core/arqit_wheels/arqit-skaplatform-monitoring/

WORKDIR /build/python/stls-core/sidecar
ENTRYPOINT ["python"]
CMD ["arqit_sidecar.py"]
