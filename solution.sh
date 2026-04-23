#!/bin/bash
set -euo pipefail

export KUBECONFIG=/home/ubuntu/.kube/config

ORG_SLUG="devops-platform"
GT_DB_PASS="7KkJeWZYkK"
LDAP_BASE_DN="dc=devops,dc=local"
LDAP_ADMIN_DN="cn=admin,dc=devops,dc=local"

echo "[solution] Inspecting active Dex, LDAP, GlitchTip, and replay-cache resources..."
kubectl get configmap dex-config -n dex >/dev/null
kubectl get deployment openldap -n ldap >/dev/null
kubectl get deployment glitchtip-runtime-cache -n glitchtip >/dev/null
kubectl get configmap glitchtip-runtime-directory -n glitchtip >/dev/null
kubectl get configmap dex-connector-bootstrap-archive -n dex >/dev/null

PG_POD=$(kubectl get pods -n glitchtip -l app.kubernetes.io/name=postgresql -o jsonpath='{.items[0].metadata.name}')
REDIS_POD=$(kubectl get pods -n glitchtip -l app=glitchtip-runtime-cache -o jsonpath='{.items[0].metadata.name}')
LDAP_POD=$(kubectl get pods -n ldap -l app=openldap -o jsonpath='{.items[0].metadata.name}')
LDAP_ADMIN_PASSWORD=$(kubectl get deployment openldap -n ldap -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="LDAP_ADMIN_PASSWORD")].value}')

gt_sql() {
  kubectl exec -n glitchtip "${PG_POD}" -- bash -c "PGPASSWORD=${GT_DB_PASS} psql -U postgres -d postgres -tAc \"$1\"" 2>/dev/null
}

redis_cli() {
  kubectl exec -n glitchtip "${REDIS_POD}" -- redis-cli "$@"
}

ldap_group_users() {
  local group="$1"
  kubectl exec -n ldap "${LDAP_POD}" -- ldapsearch -x \
    -D "${LDAP_ADMIN_DN}" -w "${LDAP_ADMIN_PASSWORD}" \
    -b "cn=${group},ou=groups,${LDAP_BASE_DN}" uniqueMember 2>/dev/null \
    | sed -n 's/^uniqueMember: uid=\([^,]*\),.*/\1/p' | sort -u
}

OWNER_USERS=$(ldap_group_users glitchtip-owners | tr '\n' ' ' | sed 's/[[:space:]]*$//')
ALL_USERS=$(ldap_group_users glitchtip-users | tr '\n' ' ' | sed 's/[[:space:]]*$//')
MEMBER_USERS=""
for username in ${ALL_USERS}; do
  case " ${OWNER_USERS} " in
    *" ${username} "*) ;;
    *) MEMBER_USERS="${MEMBER_USERS} ${username}" ;;
  esac
done
MEMBER_USERS=$(echo "${MEMBER_USERS}" | xargs)

echo "[solution] LDAP-designated owners: ${OWNER_USERS}"
echo "[solution] LDAP-designated non-owner users: ${MEMBER_USERS}"

echo "[solution] Correcting both the live runtime directory and the Dex-side archived baseline..."
OWNER_DIRECTORY_LITERAL=$(printf '%s\n' ${OWNER_USERS})
kubectl create configmap glitchtip-runtime-directory \
  -n glitchtip \
  --from-literal=directory-sync.txt="${OWNER_DIRECTORY_LITERAL}" \
  --from-literal=notes.md="Current runtime directory rebuilt from the Dex/LDAP owner group." \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl create configmap dex-connector-bootstrap-archive \
  -n dex \
  --from-literal=directory-sync.txt="${OWNER_DIRECTORY_LITERAL}" \
  --from-literal=notes.md="Connector archive aligned to current Dex/LDAP owner truth." \
  --dry-run=client -o yaml | kubectl apply -f -

echo "[solution] Rebuilding Redis replay and session state from current identity truth..."
redis_cli DEL "gt:org:${ORG_SLUG}:warm-owners" >/dev/null
for username in ${OWNER_USERS}; do
  redis_cli SADD "gt:org:${ORG_SLUG}:warm-owners" "${username}" >/dev/null
  redis_cli SET "gt:principal:${username}:session-role" owner >/dev/null
  redis_cli SET "gt:principal:${username}:effective-role" owner >/dev/null
done
for username in ${MEMBER_USERS}; do
  redis_cli SREM "gt:org:${ORG_SLUG}:warm-owners" "${username}" >/dev/null || true
  redis_cli DEL "gt:principal:${username}:session-role" "gt:principal:${username}:effective-role" >/dev/null || true
done
redis_cli SET gt:warm:last-source "corrected-dex-ldap-truth" >/dev/null

echo "[solution] Repairing durable GlitchTip organization roles..."
ORG_ID=$(gt_sql "SELECT id FROM organizations_ext_organization WHERE slug='${ORG_SLUG}' LIMIT 1;")
for username in ${OWNER_USERS}; do
  USER_ID=$(gt_sql "SELECT id FROM users_user WHERE email='${username}@devops.local' LIMIT 1;")
  if [ -n "${USER_ID}" ] && [ -n "${ORG_ID}" ]; then
    EXISTS=$(gt_sql "SELECT COUNT(*) FROM organizations_ext_organizationuser WHERE user_id=${USER_ID} AND organization_id=${ORG_ID};")
    if [ "${EXISTS}" = "0" ]; then
      gt_sql "INSERT INTO organizations_ext_organizationuser (organization_id, user_id, role, email, created, modified) VALUES (${ORG_ID}, ${USER_ID}, 3, '${username}@devops.local', NOW(), NOW());" >/dev/null
    else
      gt_sql "UPDATE organizations_ext_organizationuser SET role=3, modified=NOW() WHERE organization_id=${ORG_ID} AND user_id=${USER_ID};" >/dev/null
    fi
  fi
done

for username in ${MEMBER_USERS}; do
  gt_sql "UPDATE organizations_ext_organizationuser SET role=0, modified=NOW() WHERE user_id=(SELECT id FROM users_user WHERE email='${username}@devops.local') AND organization_id=${ORG_ID};" >/dev/null
done

echo "[solution] Forcing the cache warmer once to prove the corrected source no longer replays stale owners..."
kubectl delete job dex-directory-cache-audit-verify -n dex --ignore-not-found=true >/dev/null 2>&1 || true
if kubectl get cronjob dex-directory-cache-audit -n dex >/dev/null 2>&1; then
  kubectl create job dex-directory-cache-audit-verify --from=cronjob/dex-directory-cache-audit -n dex >/dev/null
  kubectl wait --for=condition=complete job/dex-directory-cache-audit-verify -n dex --timeout=120s >/dev/null 2>&1 || true
  kubectl delete job dex-directory-cache-audit-verify -n dex --ignore-not-found=true >/dev/null 2>&1 || true
fi
kubectl delete job glitchtip-session-profile-rollup-verify -n glitchtip --ignore-not-found=true >/dev/null 2>&1 || true
if kubectl get cronjob glitchtip-session-profile-rollup -n glitchtip >/dev/null 2>&1; then
  kubectl create job glitchtip-session-profile-rollup-verify --from=cronjob/glitchtip-session-profile-rollup -n glitchtip >/dev/null
  kubectl wait --for=condition=complete job/glitchtip-session-profile-rollup-verify -n glitchtip --timeout=120s >/dev/null 2>&1 || true
  kubectl delete job glitchtip-session-profile-rollup-verify -n glitchtip --ignore-not-found=true >/dev/null 2>&1 || true
fi

echo "[solution] Reasserting final safe state after replay verification..."
for username in ${MEMBER_USERS}; do
  redis_cli DEL "gt:principal:${username}:session-role" "gt:principal:${username}:effective-role" >/dev/null || true
  gt_sql "UPDATE organizations_ext_organizationuser SET role=0, modified=NOW() WHERE user_id=(SELECT id FROM users_user WHERE email='${username}@devops.local') AND organization_id=${ORG_ID};" >/dev/null
done
redis_cli SET gt:warm:last-source "corrected-dex-ldap-truth" >/dev/null

echo "[solution] Final Redis owner replay set:"
redis_cli SMEMBERS "gt:org:${ORG_SLUG}:warm-owners" | sort

echo "[solution] Final GlitchTip roles:"
gt_sql "SELECT u.email || '=' || ou.role FROM organizations_ext_organizationuser ou JOIN users_user u ON u.id=ou.user_id JOIN organizations_ext_organization o ON o.id=ou.organization_id WHERE o.slug='${ORG_SLUG}' ORDER BY u.email;"

echo "[solution] Dex/LDAP owner replay drift repaired."
