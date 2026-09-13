# OntoBricks on AKS

The image is the same one `deploy/azure/Dockerfile` builds — it is a plain ASGI
process in an OCI image and knows nothing about its runtime. Only these manifests
are AKS-specific.

## The one constraint that is not negotiable

**`replicas: 1`.** APScheduler runs *in-process*, so every additional replica
duplicates every scheduled build: two pods means two of each build, writing the
same graph tables concurrently. There is no leader election. `strategy: Recreate`
is set for the same reason — a `RollingUpdate` briefly runs two pods, which is
exactly the state to avoid.

Scaling out needs the scheduler extracted into its own single-replica Deployment
(or a `CronJob`) first. Until then, an HPA on this Deployment is a data-integrity
bug, not a performance win.

## What talks to what

| Concern | Mechanism |
|---|---|
| PostgreSQL auth | Microsoft Entra via **Workload Identity** — `DefaultAzureCredential` picks up the projected token with no code change |
| Registry / graph DB | `PGHOST` etc. in the ConfigMap; the schema is created by *Settings → Registry → Initialize* |
| Databricks login (optional) | `ONTOBRICKS_OIDC_*` — the client secret is a Secret, not a ConfigMap |
| LLM / embeddings (optional) | `ONTOBRICKS_LLM_*`, `ONTOBRICKS_EMBEDDING_MODEL` |

## Prerequisites

An AKS cluster with the OIDC issuer and workload identity enabled, and a
user-assigned managed identity federated to this namespace's service account:

```bash
RG=ontobricks-steve-rg LOC=westus CLUSTER=ontobricks-aks
ACR=ontobrickssteveacr NS=ontobricks IDENTITY=ontobricks-app-identity

az aks create -g $RG -n $CLUSTER --location $LOC \
    --node-count 1 --node-vm-size Standard_D2s_v5 \
    --enable-oidc-issuer --enable-workload-identity \
    --attach-acr $ACR --generate-ssh-keys
az aks get-credentials -g $RG -n $CLUSTER

# The identity that will authenticate to PostgreSQL
az identity create -g $RG -n $IDENTITY
CLIENT_ID=$(az identity show -g $RG -n $IDENTITY --query clientId -o tsv)
ISSUER=$(az aks show -g $RG -n $CLUSTER --query oidcIssuerProfile.issuerUrl -o tsv)

# Federate it to the service account these manifests create
az identity federated-credential create -g $RG \
    --identity-name $IDENTITY --name ontobricks-fedcred \
    --issuer "$ISSUER" \
    --subject "system:serviceaccount:${NS}:ontobricks" \
    --audience api://AzureADTokenExchange
```

Then make that identity a PostgreSQL principal, exactly as for any other Entra
identity (run against the `postgres` database, as an Entra admin):

```sql
SELECT * FROM pgaadauth_create_principal('<IDENTITY-NAME>', false, false);
GRANT CREATE ON DATABASE ontobricks TO "<IDENTITY-NAME>";
```

`GRANT CREATE` is needed because the app creates its own schemas — the registry
schema on *Initialize*, and the graph schema on the first Knowledge Graph build.

## Secrets

Deliberately not in this directory. Create them in the cluster:

```bash
kubectl -n $NS create secret generic ontobricks \
    --from-literal=SECRET_KEY="$(openssl rand -hex 32)" \
    --from-literal=ONTOBRICKS_OIDC_CLIENT_SECRET=... \
    --from-literal=ONTOBRICKS_LLM_API_KEY=...
```

Every key is optional except `SECRET_KEY`; the Deployment marks them
`optional: true` so a minimal install starts without the Databricks and LLM ones.

## Deploy

```bash
TAG=$(git rev-parse --short HEAD)
az acr build -r $ACR -t ontobricks:$TAG -f deploy/azure/Dockerfile .

cd deploy/azure/k8s
kubectl apply -k .                      # namespace, SA, ConfigMap, Service, Deployment
kubectl -n $NS set image deployment/ontobricks \
    ontobricks=$ACR.azurecr.io/ontobricks:$TAG
kubectl -n $NS rollout status deployment/ontobricks
```

Edit `configmap.yaml` for your `PGHOST` and the workload-identity client id in
`serviceaccount.yaml` before the first apply.

## Exposure

`service.yaml` is a `ClusterIP`, so nothing is public until you choose how to
expose it. Pick one:

* `kubectl -n $NS port-forward svc/ontobricks 8000:80` — verify before exposing.
* Change the Service to `type: LoadBalancer` for a quick public IP.
* An Ingress, if the cluster has a controller. `ONTOBRICKS_OIDC_REDIRECT_URI`
  must match whatever hostname you land on, and the Databricks app registration
  must list that exact URI.

## Other Azure clouds

`overlays/china/` deploys the same base to Azure China (portal.azure.cn), which is
a separate cloud with its own Entra authority and data-plane domains. It patches
only the endpoints; every structural constraint above is inherited. See its README
for what needed a code change and what to verify first.

## Decommissioning the Container Apps deployment

The earlier deployment (`ontobricks-app` in `ontobricks-steve-rg`) is independent.
Once AKS is serving, remove it and its environment so there is one deployment,
not two:

```bash
az containerapp delete -n ontobricks-app -g $RG --yes
az containerapp env delete -n ontobricks-env-vnet -g $RG --yes
```

The Postgres server, ACR and the Databricks app registration are shared and
should stay.
