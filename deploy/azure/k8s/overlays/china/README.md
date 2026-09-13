# OntoBricks on Azure China (portal.azure.cn)

Azure China is a **separate cloud**, not a region: its own ARM, its own Entra
authority, its own data-plane domains, and its own accounts. A global Entra
identity cannot sign in, and a global subscription is not visible there.

## What needed code changes, and what did not

Two values were hardcoded to the global cloud, and both failed in a way that
pointed at the wrong culprit:

| | Global | Azure China |
|---|---|---|
| Postgres Entra audience | `ossrdbms-aad.database.windows.net` | `ossrdbms-aad.database.chinacloudapi.cn` |
| Auth-mode inference suffix | `.postgres.database.azure.com` | `.postgres.database.chinacloudapi.cn` |

The audience is a fixed per-cloud constant, **not** derivable by pattern from the
server name — the global server is `*.postgres.database.azure.com` while its
audience is `database.windows.net`. Requesting the wrong one is rejected at
connection time and reads as a bad credential. The inference was worse: an unmatched
host fell through to `lakebase`, presenting a Databricks Lakebase JWT to Azure
Postgres.

Both are now looked up from one table keyed on the `PGHOST` suffix
(`back/core/postgres/EntraCredential.py`), so **setting `PGHOST` is enough** — no
scope variable, and the inference and the audience cannot drift apart.

Everything else needed no code change:

* **Entra identity** — `DefaultAzureCredential` reads `AZURE_AUTHORITY_HOST`
  itself, so the overlay sets it and the code is untouched.
* **The image** — a plain ASGI process; it does not know which cloud it is in.
* **The manifests** — this is a kustomize *overlay*. Single replica, `Recreate`,
  workload identity, probes and the security context all come from `../..`,
  because they are properties of the application, not of a cloud.

## Verify before you rely on it

I have no Azure China access, so the following are from documentation and the
shape of the other clouds, **not** tested end to end. Check each on your
subscription before planning around it:

1. **The Postgres Entra audience.** The value above is what the table uses. If a
   connection fails Entra authentication, override it without a redeploy of the
   image: `ONTOBRICKS_PG_TOKEN_SCOPE=...` in the ConfigMap.
2. **AKS Workload Identity availability.** Azure China feature parity lags. If
   `--enable-workload-identity` is unavailable, fall back to
   `ONTOBRICKS_PG_AUTH=password` with `PGPASSWORD` in the Secret — supported and
   needs no identity federation.
3. **Region names.** `chinanorth3` / `chinaeast3` are the usual ones; not every
   service is in every region.
4. **Azure Databricks** is optional here. In China it is `*.databricks.azure.cn`.
   Left unset, OntoBricks runs entirely on PostgreSQL — no login, no Unity
   Catalog, no LLM features.

## Provisioning

```bash
# 1. Point the CLI at the right cloud, then sign in with a CHINA account.
az cloud set --name AzureChinaCloud
az login                                    # interactive; a global account fails

RG=ontobricks-cn-rg
LOC=chinanorth3
ACR=ontobrickscn                            # becomes $ACR.azurecr.cn
PG=ontobricks-pg-cn                         # becomes $PG.postgres.database.chinacloudapi.cn
CLUSTER=ontobricks-aks-cn
IDENTITY=ontobricks-app-identity
NS=ontobricks
ADMIN=$(az ad signed-in-user show --query userPrincipalName -o tsv)
ADMIN_OID=$(az ad signed-in-user show --query id -o tsv)

az group create -n $RG -l $LOC

# 2. Registry.
az acr create -g $RG -n $ACR --sku Standard

# 3. Azure Database for PostgreSQL, Entra-only (no password to leak).
az postgres flexible-server create -g $RG -n $PG -l $LOC \
    --tier Burstable --sku-name Standard_B2s --version 16 \
    --storage-size 32 --public-access 0.0.0.0 \
    --microsoft-entra-auth Enabled --password-auth Disabled \
    --admin-object-id "$ADMIN_OID" --admin-display-name "$ADMIN" \
    --admin-type User
az postgres flexible-server db create -g $RG -s $PG -d ontobricks
```

`--public-access 0.0.0.0` means "allow Azure services", not "allow the world" —
but check for a policy denying broad firewall rules, as one exists in some tenants.
Restricting to the AKS egress IP is better; get it from the cluster's outbound IP
once created.

```bash
# 4. AKS with the OIDC issuer and workload identity.
az aks create -g $RG -n $CLUSTER -l $LOC \
    --node-count 1 --node-vm-size Standard_D2s_v5 \
    --enable-oidc-issuer --enable-workload-identity \
    --attach-acr $ACR --generate-ssh-keys
az aks get-credentials -g $RG -n $CLUSTER

# 5. The identity that authenticates to PostgreSQL.
az identity create -g $RG -n $IDENTITY
CLIENT_ID=$(az identity show -g $RG -n $IDENTITY --query clientId -o tsv)
ISSUER=$(az aks show -g $RG -n $CLUSTER --query oidcIssuerProfile.issuerUrl -o tsv)
az identity federated-credential create -g $RG \
    --identity-name $IDENTITY --name ontobricks-fedcred \
    --issuer "$ISSUER" \
    --subject "system:serviceaccount:${NS}:ontobricks" \
    --audience api://AzureADTokenExchange
```

Then make the identity a Postgres principal, connecting as the Entra admin against
the **`postgres`** database (roles are cluster-wide, the function lives there):

```sql
SELECT * FROM pgaadauth_create_principal('ontobricks-app-identity', false, false);
GRANT CREATE ON DATABASE ontobricks TO "ontobricks-app-identity";
```

`GRANT CREATE` matters: the app creates its own schemas — the registry schema on
*Initialize*, and the graph schema on the first Knowledge Graph build.

## Deploy

```bash
TAG=$(git rev-parse --short HEAD)
az acr build -r $ACR -t ontobricks:$TAG -f deploy/azure/Dockerfile .

cd deploy/azure/k8s/overlays/china
kubectl --context $CLUSTER apply -k .
kubectl --context $CLUSTER -n $NS create secret generic ontobricks \
    --from-literal=SECRET_KEY="$(openssl rand -hex 32)"
kubectl --context $CLUSTER -n $NS rollout status deployment/ontobricks
kubectl --context $CLUSTER -n $NS port-forward svc/ontobricks 8000:80
```

Then `http://localhost:8000/health` — `status` will be `warning` with the registry
uninitialized, which is correct on a fresh deployment. Settings → Registry →
**Initialize** creates the tables.

## Sanity check the cloud wiring first

Before deploying, confirm the code picks the China values from your host:

```bash
PGHOST=$PG.postgres.database.chinacloudapi.cn uv run --frozen python -c "
from back.core.postgres.EntraCredential import resolve_pg_token_scope
from back.core.databricks.lakebase.LakebaseAuth import resolve_pg_auth_mode
print('audience:', resolve_pg_token_scope())
print('auth mode:', resolve_pg_auth_mode())
"
```

Expect the `chinacloudapi.cn` audience and `entra`. Anything else means the suffix
table does not recognise your host, and `ONTOBRICKS_PG_TOKEN_SCOPE` plus
`ONTOBRICKS_PG_AUTH=entra` will force it while you report the gap.
