#!/bin/bash
set -eo pipefail

exec 1> >(stdbuf -oL cat) 2>&1
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

echo "[setup] Waiting for k3s node to be Ready..."
until kubectl get nodes 2>/dev/null | grep -q " Ready"; do sleep 2; done
echo "[setup] k3s is Ready."

mkdir -p /home/ubuntu/.kube
cp /etc/rancher/k3s/k3s.yaml /home/ubuntu/.kube/config
chown -R ubuntu:ubuntu /home/ubuntu/.kube
chmod 600 /home/ubuntu/.kube/config

echo "[setup] Importing task images..."
CTR="ctr --address /run/k3s/containerd/containerd.sock -n k8s.io"
until [ -S /run/k3s/containerd/containerd.sock ]; do sleep 2; done
sleep 5
for img in /var/lib/rancher/k3s/agent/images/*.tar; do
  imgname=$(basename "$img")
  echo "[setup] Importing ${imgname}..."
  for attempt in $(seq 1 5); do
    if $CTR images import "$img" 2>&1; then
      echo "[setup] ${imgname} imported."
      break
    fi
    echo "[setup] Retry ${attempt}/5 for ${imgname}..."
    sleep 10
  done
done

echo "[setup] Scaling down non-essential workloads..."
for ns in bleater monitoring observability harbor argocd mattermost; do
  for dep in $(kubectl get deployments -n "$ns" -o name 2>/dev/null); do
    kubectl scale "$dep" -n "$ns" --replicas=0 2>/dev/null || true
  done
  for sts in $(kubectl get statefulsets -n "$ns" -o name 2>/dev/null); do
    kubectl scale "$sts" -n "$ns" --replicas=0 2>/dev/null || true
  done
done

echo "[setup] Waiting for k3s API to stabilize..."
sleep 15
until kubectl get nodes >/dev/null 2>&1; do sleep 5; done
sleep 10
until kubectl get nodes >/dev/null 2>&1; do sleep 3; done

NODE_NAME=$(kubectl get nodes -o jsonpath='{.items[0].metadata.name}')
kubectl taint node "$NODE_NAME" node.kubernetes.io/unreachable- 2>/dev/null || true
kubectl taint node "$NODE_NAME" node.kubernetes.io/not-ready- 2>/dev/null || true
kubectl taint node "$NODE_NAME" node.kubernetes.io/disk-pressure- 2>/dev/null || true
kubectl delete validatingwebhookconfiguration ingress-nginx-admission 2>/dev/null || true

###############################################
# Constants and helpers
###############################################
GLITCHTIP_URL="http://glitchtip.devops.local"
DEX_URL="http://dex.dex.svc.cluster.local:5556"
DEX_PUBLIC_URL="http://dex.devops.local"
LDAP_BASE_DN="dc=devops,dc=local"
LDAP_ADMIN_DN="cn=admin,dc=devops,dc=local"
LDAP_ADMIN_PASSWORD="ldap-admin-2026"
DEX_CLIENT_SECRET="dex-glitchtip-secret-$(head -c 12 /dev/urandom | od -A n -t x1 | tr -d ' \n')"
GT_DB_PASS="7KkJeWZYkK"
GT_DB_USER="postgres"
GT_DB_NAME="postgres"
ORG_SLUG="devops-platform"
USER_PASS="DevOps2024!"

gt_pg_pod() {
  kubectl get pods -n glitchtip -l app.kubernetes.io/name=postgresql -o jsonpath='{.items[0].metadata.name}' 2>/dev/null
}

gt_sql() {
  local pod
  pod=$(gt_pg_pod)
  kubectl exec -n glitchtip "${pod}" -- bash -c \
    "PGPASSWORD=${GT_DB_PASS} psql -U ${GT_DB_USER} -d ${GT_DB_NAME} -tAc \"$1\"" 2>/dev/null
}

wait_for_pod_ready() {
  local namespace="$1"
  local selector="$2"
  local timeout="${3:-180s}"
  kubectl wait --for=condition=ready pod -n "$namespace" -l "$selector" --timeout="$timeout"
}

###############################################
# GlitchTip baseline
###############################################
echo "[setup] Waiting for GlitchTip..."
kubectl wait --for=condition=ready pod -l app.kubernetes.io/name=glitchtip -n glitchtip --timeout=300s 2>/dev/null || true

for i in $(seq 1 60); do
  HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "${GLITCHTIP_URL}" 2>/dev/null || echo "000")
  if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "301" ] || [ "$HTTP_CODE" = "302" ]; then
    echo "[setup] GlitchTip responding."
    break
  fi
  sleep 5
done

GT_WEB_POD=$(kubectl get pods -n glitchtip -l app.kubernetes.io/name=glitchtip,app.kubernetes.io/component=web -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
kubectl exec -n glitchtip "${GT_WEB_POD}" -- python manage.py migrate --noinput 2>/dev/null || true

###############################################
# LDAP: truthful upstream identity source
###############################################
echo "[setup] Deploying LDAP identity source..."
kubectl create namespace ldap --dry-run=client -o yaml | kubectl apply -f -

kubectl apply -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: openldap
  namespace: ldap
  labels:
    app: openldap
spec:
  replicas: 1
  selector:
    matchLabels:
      app: openldap
  template:
    metadata:
      labels:
        app: openldap
    spec:
      containers:
      - name: openldap
        image: docker.io/osixia/openldap:1.5.0
        imagePullPolicy: IfNotPresent
        ports:
        - containerPort: 389
        env:
        - name: LDAP_ORGANISATION
          value: "Nebula DevOps"
        - name: LDAP_DOMAIN
          value: "devops.local"
        - name: LDAP_ADMIN_PASSWORD
          value: "${LDAP_ADMIN_PASSWORD}"
---
apiVersion: v1
kind: Service
metadata:
  name: openldap
  namespace: ldap
spec:
  selector:
    app: openldap
  ports:
  - name: ldap
    port: 389
    targetPort: 389
EOF

wait_for_pod_ready ldap app=openldap 180s
LDAP_POD=$(kubectl get pods -n ldap -l app=openldap -o jsonpath='{.items[0].metadata.name}')

cat > /tmp/glitchtip-users.ldif <<'LDIF'
dn: ou=people,dc=devops,dc=local
objectClass: organizationalUnit
ou: people

dn: ou=groups,dc=devops,dc=local
objectClass: organizationalUnit
ou: groups

dn: uid=mira,ou=people,dc=devops,dc=local
objectClass: inetOrgPerson
objectClass: organizationalPerson
objectClass: person
objectClass: top
uid: mira
cn: Mira Engineer
sn: Engineer
mail: mira@devops.local
userPassword: DevOps2024!

dn: uid=noah,ou=people,dc=devops,dc=local
objectClass: inetOrgPerson
objectClass: organizationalPerson
objectClass: person
objectClass: top
uid: noah
cn: Noah Engineer
sn: Engineer
mail: noah@devops.local
userPassword: DevOps2024!

dn: uid=kai,ou=people,dc=devops,dc=local
objectClass: inetOrgPerson
objectClass: organizationalPerson
objectClass: person
objectClass: top
uid: kai
cn: Kai Engineer
sn: Engineer
mail: kai@devops.local
userPassword: DevOps2024!

dn: uid=lena,ou=people,dc=devops,dc=local
objectClass: inetOrgPerson
objectClass: organizationalPerson
objectClass: person
objectClass: top
uid: lena
cn: Lena Engineer
sn: Engineer
mail: lena@devops.local
userPassword: DevOps2024!

dn: uid=omar,ou=people,dc=devops,dc=local
objectClass: inetOrgPerson
objectClass: organizationalPerson
objectClass: person
objectClass: top
uid: omar
cn: Omar Engineer
sn: Engineer
mail: omar@devops.local
userPassword: DevOps2024!

dn: cn=glitchtip-owners,ou=groups,dc=devops,dc=local
objectClass: groupOfUniqueNames
objectClass: top
cn: glitchtip-owners
uniqueMember: uid=mira,ou=people,dc=devops,dc=local
uniqueMember: uid=noah,ou=people,dc=devops,dc=local

dn: cn=glitchtip-users,ou=groups,dc=devops,dc=local
objectClass: groupOfUniqueNames
objectClass: top
cn: glitchtip-users
uniqueMember: uid=mira,ou=people,dc=devops,dc=local
uniqueMember: uid=noah,ou=people,dc=devops,dc=local
uniqueMember: uid=kai,ou=people,dc=devops,dc=local
uniqueMember: uid=lena,ou=people,dc=devops,dc=local
uniqueMember: uid=omar,ou=people,dc=devops,dc=local
LDIF

kubectl cp /tmp/glitchtip-users.ldif "ldap/${LDAP_POD}:/tmp/glitchtip-users.ldif"
kubectl exec -n ldap "${LDAP_POD}" -- ldapadd -x -D "${LDAP_ADMIN_DN}" -w "${LDAP_ADMIN_PASSWORD}" -f /tmp/glitchtip-users.ldif >/dev/null 2>&1 || true

###############################################
# Dex: OIDC facade backed by LDAP
###############################################
echo "[setup] Deploying Dex backed by LDAP..."
kubectl create namespace dex --dry-run=client -o yaml | kubectl apply -f -

kubectl apply -f - <<EOF
apiVersion: v1
kind: ConfigMap
metadata:
  name: dex-config
  namespace: dex
  labels:
    app: dex
data:
  config.yaml: |
    issuer: ${DEX_PUBLIC_URL}
    storage:
      type: memory
    web:
      http: 0.0.0.0:5556
    oauth2:
      skipApprovalScreen: true
    staticClients:
    - id: glitchtip
      name: GlitchTip
      secret: ${DEX_CLIENT_SECRET}
      redirectURIs:
      - http://glitchtip.devops.local/accounts/oidc/login/callback/
      - http://glitchtip.devops.local/*
    connectors:
    - type: ldap
      id: nebula-ldap
      name: Nebula LDAP
      config:
        host: openldap.ldap.svc.cluster.local:389
        insecureNoSSL: true
        bindDN: ${LDAP_ADMIN_DN}
        bindPW: ${LDAP_ADMIN_PASSWORD}
        usernamePrompt: Email
        userSearch:
          baseDN: ou=people,${LDAP_BASE_DN}
          filter: "(objectClass=inetOrgPerson)"
          username: mail
          idAttr: uid
          emailAttr: mail
          nameAttr: cn
        groupSearch:
          baseDN: ou=groups,${LDAP_BASE_DN}
          filter: "(objectClass=groupOfUniqueNames)"
          userMatchers:
          - userAttr: DN
            groupAttr: uniqueMember
          nameAttr: cn
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: dex
  namespace: dex
  labels:
    app: dex
spec:
  replicas: 1
  selector:
    matchLabels:
      app: dex
  template:
    metadata:
      labels:
        app: dex
    spec:
      containers:
      - name: dex
        image: docker.io/dexidp/dex:v2.41.1
        imagePullPolicy: IfNotPresent
        command: ["/usr/local/bin/dex", "serve", "/etc/dex/config.yaml"]
        ports:
        - containerPort: 5556
        volumeMounts:
        - name: config
          mountPath: /etc/dex
      volumes:
      - name: config
        configMap:
          name: dex-config
---
apiVersion: v1
kind: Service
metadata:
  name: dex
  namespace: dex
spec:
  selector:
    app: dex
  ports:
  - name: http
    port: 5556
    targetPort: 5556
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: dex
  namespace: dex
spec:
  ingressClassName: nginx
  rules:
  - host: dex.devops.local
    http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: dex
            port:
              number: 5556
EOF

kubectl rollout status deployment/dex -n dex --timeout=180s || true
wait_for_pod_ready dex app=dex 180s || true

###############################################
# GlitchTip OIDC config and org data
###############################################
echo "[setup] Configuring GlitchTip Dex OIDC metadata..."
kubectl apply -f - <<EOF
apiVersion: v1
kind: ConfigMap
metadata:
  name: glitchtip-dex-oidc-config
  namespace: glitchtip
  labels:
    app: glitchtip
    component: dex-oidc
data:
  ENABLE_OPEN_ID_CONNECT: "true"
  OPENID_CONNECT_URL: "${DEX_PUBLIC_URL}/.well-known/openid-configuration"
  OPENID_CONNECT_CLIENT_ID: "glitchtip"
  OPENID_CONNECT_CLIENT_SECRET: "${DEX_CLIENT_SECRET}"
  OPENID_CONNECT_SCOPE: "openid profile email groups"
  GLITCHTIP_OIDC_OWNER_GROUP: "glitchtip-owners"
  GLITCHTIP_OIDC_MEMBER_GROUP: "glitchtip-users"
  GLITCHTIP_IDENTITY_PROVIDER: "dex-ldap"
EOF

GT_CONTAINER=$(kubectl get deployment glitchtip-web -n glitchtip -o jsonpath='{.spec.template.spec.containers[0].name}' 2>/dev/null || echo glitchtip)
kubectl patch deployment glitchtip-web -n glitchtip --type strategic -p '{
  "spec": {
    "template": {
      "spec": {
        "containers": [{
          "name": "'"${GT_CONTAINER}"'",
          "envFrom": [
            {"configMapRef": {"name": "glitchtip"}},
            {"secretRef": {"name": "glitchtip"}},
            {"configMapRef": {"name": "glitchtip-dex-oidc-config"}}
          ]
        }]
      }
    }
  }
}' 2>/dev/null || true
kubectl rollout restart deployment glitchtip-web -n glitchtip 2>/dev/null || true
kubectl rollout status deployment glitchtip-web -n glitchtip --timeout=180s || true

echo "[setup] Creating GlitchTip users and initial over-privileged org state..."
DUMMY_PW='pbkdf2_sha256\$600000\$salt\$hash'
gt_sql "INSERT INTO users_user (email, password, is_staff, is_superuser, is_active, created, name, subscribe_by_default, options)
  VALUES ('admin@devops.local', '${DUMMY_PW}', true, true, true, NOW(), 'Admin', true, '{}')
  ON CONFLICT (email) DO NOTHING;"

gt_sql "INSERT INTO organizations_ext_organization (name, slug, created, modified, is_active, is_accepting_events, open_membership, scrub_ip_addresses, event_throttle_rate, stripe_customer_id)
  VALUES ('DevOps Platform', '${ORG_SLUG}', NOW(), NOW(), true, true, false, false, 0, '')
  ON CONFLICT (slug) DO NOTHING;"

ORG_ID=$(gt_sql "SELECT id FROM organizations_ext_organization WHERE slug='${ORG_SLUG}' LIMIT 1;")
ADMIN_ID=$(gt_sql "SELECT id FROM users_user WHERE email='admin@devops.local' LIMIT 1;")
gt_sql "INSERT INTO organizations_ext_organizationuser (organization_id, user_id, role, email, created, modified)
  VALUES (${ORG_ID}, ${ADMIN_ID}, 3, 'admin@devops.local', NOW(), NOW())
  ON CONFLICT DO NOTHING;" || true

for username in mira noah kai lena omar; do
  gt_sql "INSERT INTO users_user (email, password, is_staff, is_superuser, is_active, created, name, subscribe_by_default, options)
    VALUES ('${username}@devops.local', '${DUMMY_PW}', false, false, true, NOW(), '${username}', true, '{}')
    ON CONFLICT (email) DO NOTHING;"
  USER_ID=$(gt_sql "SELECT id FROM users_user WHERE email='${username}@devops.local' LIMIT 1;")
  gt_sql "INSERT INTO organizations_ext_organizationuser (organization_id, user_id, role, email, created, modified)
    VALUES (${ORG_ID}, ${USER_ID}, 3, '${username}@devops.local', NOW(), NOW())
    ON CONFLICT (user_id, organization_id) DO UPDATE SET role = 3, email = EXCLUDED.email;" || true
  echo "[setup] ${username}@devops.local is currently owner in GlitchTip."
done

###############################################
# Redis-backed replay cache and cache-warmer drift
###############################################
echo "[setup] Creating Redis-backed authorization replay cache..."
kubectl apply -f - <<'EOF'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: glitchtip-auth-replay-cache
  namespace: glitchtip
  labels:
    app: glitchtip-auth-replay-cache
    component: auth-cache
spec:
  replicas: 1
  selector:
    matchLabels:
      app: glitchtip-auth-replay-cache
  template:
    metadata:
      labels:
        app: glitchtip-auth-replay-cache
    spec:
      containers:
      - name: redis
        image: docker.io/redis:7-alpine
        imagePullPolicy: IfNotPresent
        ports:
        - containerPort: 6379
---
apiVersion: v1
kind: Service
metadata:
  name: glitchtip-auth-replay-cache
  namespace: glitchtip
spec:
  selector:
    app: glitchtip-auth-replay-cache
  ports:
  - name: redis
    port: 6379
    targetPort: 6379
EOF

kubectl rollout status deployment/glitchtip-auth-replay-cache -n glitchtip --timeout=120s || true
wait_for_pod_ready glitchtip app=glitchtip-auth-replay-cache 120s
REDIS_POD=$(kubectl get pods -n glitchtip -l app=glitchtip-auth-replay-cache -o jsonpath='{.items[0].metadata.name}')

redis_exec() {
  kubectl exec -n glitchtip "${REDIS_POD}" -- redis-cli "$@"
}

redis_exec DEL authz:glitchtip:devops-platform:owner-replay >/dev/null
for username in mira noah kai lena omar; do
  redis_exec SADD authz:glitchtip:devops-platform:owner-replay "$username" >/dev/null
  redis_exec SET "authz:session:${username}:role" owner >/dev/null
  redis_exec SET "authz:effective:${username}:role" owner >/dev/null
done
redis_exec SET authz:replay:last_source "migration-bootstrap-2025q1" >/dev/null

kubectl apply -f - <<'EOF'
apiVersion: v1
kind: ConfigMap
metadata:
  name: glitchtip-auth-bootstrap-snapshot
  namespace: glitchtip
  labels:
    app: glitchtip
    component: auth-replay
data:
  owner-replay.txt: |
    mira
    noah
    kai
    lena
    omar
  notes.md: |
    # GlitchTip Authorization Bootstrap Snapshot
    Captured during the old SSO migration and used by the session warmer when
    it believes Redis role material needs to be rebuilt. Treat this as a replay
    input, not as current upstream LDAP truth.
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: glitchtip-auth-replay-script
  namespace: glitchtip
  labels:
    app: glitchtip
    component: auth-replay
data:
  replay.sh: |
    #!/bin/bash
    set -euo pipefail
    export PATH="/tools:${PATH}"
    REDIS_HOST="${REDIS_HOST:-glitchtip-auth-replay-cache}"
    ORG_SLUG="${ORG_SLUG:-devops-platform}"
    SNAPSHOT_FILE="${SNAPSHOT_FILE:-/snapshot/owner-replay.txt}"

    redis-cli -h "${REDIS_HOST}" DEL "authz:glitchtip:${ORG_SLUG}:owner-replay" >/dev/null
    while IFS= read -r username; do
      username="$(echo "${username}" | tr -d '[:space:]')"
      [ -z "${username}" ] && continue
      redis-cli -h "${REDIS_HOST}" SADD "authz:glitchtip:${ORG_SLUG}:owner-replay" "${username}" >/dev/null
      redis-cli -h "${REDIS_HOST}" SET "authz:session:${username}:role" owner >/dev/null
      redis-cli -h "${REDIS_HOST}" SET "authz:effective:${username}:role" owner >/dev/null
      PGPASSWORD="${GT_DB_PASS}" psql -h glitchtip-postgresql -U postgres -d postgres -v ON_ERROR_STOP=1 -c "
        UPDATE organizations_ext_organizationuser
        SET role = 3, modified = NOW()
        WHERE user_id = (SELECT id FROM users_user WHERE email = '${username}@devops.local')
          AND organization_id = (SELECT id FROM organizations_ext_organization WHERE slug = '${ORG_SLUG}');
      " >/dev/null
    done < "${SNAPSHOT_FILE}"
    redis-cli -h "${REDIS_HOST}" SET authz:replay:last_source "bootstrap-snapshot" >/dev/null
    echo "Authorization replay completed from ${SNAPSHOT_FILE}"
EOF

kubectl apply -f - <<'EOF'
apiVersion: batch/v1
kind: CronJob
metadata:
  name: glitchtip-auth-session-warm
  namespace: glitchtip
  labels:
    app: glitchtip
    component: session-cache
  annotations:
    description: "Warms GlitchTip authorization/session cache after SSO sync"
    managed-by: "platform-auth-lifecycle"
spec:
  schedule: "*/1 * * * *"
  successfulJobsHistoryLimit: 1
  failedJobsHistoryLimit: 1
  jobTemplate:
    spec:
      backoffLimit: 1
      activeDeadlineSeconds: 90
      template:
        metadata:
          labels:
            app: glitchtip
            job: auth-session-warm
        spec:
          restartPolicy: Never
          volumes:
          - name: replay-script
            configMap:
              name: glitchtip-auth-replay-script
              defaultMode: 0755
          - name: bootstrap-snapshot
            configMap:
              name: glitchtip-auth-bootstrap-snapshot
          - name: tools
            emptyDir: {}
          initContainers:
          - name: copy-redis-cli
            image: docker.io/redis:7-alpine
            imagePullPolicy: IfNotPresent
            command: ["/bin/sh", "-c", "cp /usr/local/bin/redis-cli /tools/redis-cli && chmod +x /tools/redis-cli"]
            volumeMounts:
            - name: tools
              mountPath: /tools
          containers:
          - name: warmer
            image: docker.io/bitnamilegacy/postgresql:17.0.0-debian-12-r11
            imagePullPolicy: IfNotPresent
            command: ["/bin/bash", "/scripts/replay.sh"]
            env:
            - name: GT_DB_PASS
              valueFrom:
                secretKeyRef:
                  name: glitchtip-postgresql
                  key: postgres-password
            - name: ORG_SLUG
              value: "devops-platform"
            - name: REDIS_HOST
              value: "glitchtip-auth-replay-cache"
            volumeMounts:
            - name: replay-script
              mountPath: /scripts
            - name: bootstrap-snapshot
              mountPath: /snapshot
            - name: tools
              mountPath: /tools
EOF

kubectl apply -f - <<'EOF'
apiVersion: batch/v1
kind: CronJob
metadata:
  name: glitchtip-ldap-sync-observer
  namespace: glitchtip
  labels:
    app: glitchtip
    component: diagnostics
spec:
  schedule: "*/5 * * * *"
  successfulJobsHistoryLimit: 1
  failedJobsHistoryLimit: 1
  jobTemplate:
    spec:
      template:
        spec:
          restartPolicy: Never
          containers:
          - name: observer
            image: docker.io/curlimages/curl:8.7.1
            imagePullPolicy: IfNotPresent
            command: ["/bin/sh", "-c", "echo ldap sync observer: no changes applied"]
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: glitchtip-auth-diagnostics
  namespace: glitchtip
  labels:
    app: glitchtip
    component: documentation
data:
  investigation-notes.md: |
    # Auth Drift Notes
    Dex and LDAP should be checked, but recent evidence suggests incoming LDAP
    group truth is no longer the obvious problem. Watch for state that survives
    org membership cleanup and then replays old owner decisions.
EOF

echo "[setup] Running the broken replay once..."
kubectl create job glitchtip-auth-session-warm-prime --from=cronjob/glitchtip-auth-session-warm -n glitchtip 2>/dev/null || true
kubectl wait --for=condition=complete job/glitchtip-auth-session-warm-prime -n glitchtip --timeout=120s 2>/dev/null || true

###############################################
# Strip clues and save grader info
###############################################
for res in \
  configmap/glitchtip-dex-oidc-config \
  configmap/glitchtip-auth-bootstrap-snapshot \
  configmap/glitchtip-auth-replay-script \
  configmap/glitchtip-auth-diagnostics \
  cronjob/glitchtip-auth-session-warm \
  cronjob/glitchtip-ldap-sync-observer; do
  kubectl annotate "$res" -n glitchtip kubectl.kubernetes.io/last-applied-configuration- 2>/dev/null || true
done
kubectl annotate configmap/dex-config -n dex kubectl.kubernetes.io/last-applied-configuration- 2>/dev/null || true

cat > /root/.setup_info <<SETUP_EOF
GLITCHTIP_URL=${GLITCHTIP_URL}
DEX_URL=${DEX_URL}
DEX_PUBLIC_URL=${DEX_PUBLIC_URL}
DEX_CLIENT_SECRET=${DEX_CLIENT_SECRET}
LDAP_BASE_DN=${LDAP_BASE_DN}
LDAP_ADMIN_DN=${LDAP_ADMIN_DN}
LDAP_ADMIN_PASSWORD=${LDAP_ADMIN_PASSWORD}
ORG_SLUG=${ORG_SLUG}
OWNER_USERS=mira,noah
MEMBER_USERS=kai,lena,omar
USER_PASS=${USER_PASS}
GT_DB_PASS=${GT_DB_PASS}
GT_DB_USER=${GT_DB_USER}
GT_DB_NAME=${GT_DB_NAME}
REPLAY_CRONJOB=glitchtip-auth-session-warm
REPLAY_CACHE_SERVICE=glitchtip-auth-replay-cache
SETUP_EOF
chmod 600 /root/.setup_info

echo "[setup] ============================================"
echo "[setup] Setup complete. Dex/LDAP is truthful; Redis replay drift is active."
echo "[setup] ============================================"
