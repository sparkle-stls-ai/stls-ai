# samples/peer/18_naas_federation/app/Dockerfile (issue #38)
# ------------------------------------------------------------------------
# One image, three entry points (seller.ts / buyer.ts / reply_agent.ts) --
# which one runs is picked by the Deployment/Job's own `command`/`args`
# (see k8s/*.yaml). Build context MUST be the repo root (needs
# node/stls-core as well as this sample's own app/ directory):
#
#   docker build -f samples/peer/18_naas_federation/app/Dockerfile \
#     -t naas18-app:dev .
#   minikube image load naas18-app:dev
#
# Mirrors the real repo's own relative depth (app/package.json depends
# on "file:../../../../node/stls-core") -- a flattened layout here
# would silently break that path and fail npm install.
FROM node:20-slim
WORKDIR /build

COPY node/stls-core ./node/stls-core
RUN cd node/stls-core && npm install && npm run build

COPY samples/peer/18_naas_federation/app ./samples/peer/18_naas_federation/app
RUN cd samples/peer/18_naas_federation/app && npm install

WORKDIR /build/samples/peer/18_naas_federation/app
ENTRYPOINT ["npx", "tsx"]
CMD ["seller.ts"]
