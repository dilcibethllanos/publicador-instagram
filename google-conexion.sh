# Pega estos comandos en Google Cloud Shell (el icono ">_" de arriba a la derecha en console.cloud.google.com).
# 1) Cambia SOLO la línea REPO por tu usuario y repositorio de GitHub, tal como aparecen en la dirección:
#    github.com/USUARIO/REPOSITORIO
REPO="USUARIO/REPOSITORIO"

PROJECT_ID="ia-woek"
SA="publicador@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud config set project "$PROJECT_ID"
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format="value(projectNumber)")

gcloud services enable iamcredentials.googleapis.com sts.googleapis.com drive.googleapis.com

gcloud iam workload-identity-pools create github-pool \
  --location=global --display-name="GitHub"

gcloud iam workload-identity-pools providers create-oidc github-provider \
  --location=global --workload-identity-pool=github-pool --display-name="GitHub Actions" \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
  --attribute-condition="assertion.repository=='${REPO}'"

gcloud iam service-accounts add-iam-policy-binding "$SA" \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github-pool/attribute.repository/${REPO}"

echo ""
echo "Guarda estos dos valores como secretos en GitHub:"
echo "WIF_PROVIDER=projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github-pool/providers/github-provider"
echo "WIF_SERVICE_ACCOUNT=${SA}"
