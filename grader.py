#!/usr/bin/env python3
"""
Grader for glitchtip-dex-ldap-owner-replay-drift.

The task is intentionally not "repair Dex and stop there". Dex and LDAP should
be truthful. The dangerous state is GlitchTip's Redis-backed authorization
replay path plus a cache warmer that can repopulate stale owner decisions.

Equal-weight objectives:
1. identity_replay_alignment
   - Dex still points at LDAP, LDAP owners are exactly mira+noah, and the
     replay bootstrap source no longer contradicts LDAP truth.
2. existing_glitchtip_roles_correct
   - Current GlitchTip org roles preserve mira+noah owners and demote
     kai/lena/omar.
3. fresh_sso_claims_match_runtime
   - A fresh Dex/LDAP-backed login shows correct groups, and GlitchTip runtime
     roles match those claims for an admin and a non-admin.
4. redis_replay_source_clean
   - Redis replay/session/effective-role state no longer contains stale owner
     state for non-admins, and the bootstrap replay source is corrected or gone.
5. replay_cycle_durable
   - After a forced replay/cache-warm cycle and a durability window, non-admins
     are not re-promoted.
6. stale_sessions_invalidated
   - Cached effective/session role keys cannot continue to represent older
     privileged sessions for non-admin users.
"""

import base64
import http.cookiejar
import json
import os
import re
import shlex
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    from apex_arena._types import GradingResult
except ImportError:
    class GradingResult:
        def __init__(self, score, subscores=None, weights=None, feedback=None):
            self.score = score
            self.subscores = subscores or {}
            self.weights = weights or {}
            self.feedback = feedback or ""


OWNER_USERS = ["mira", "noah"]
MEMBER_USERS = ["kai", "lena", "omar"]
USER_PASSWORD = "DevOps2024!"


def run_cmd(cmd, timeout=30):
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", "timeout"
    except Exception as exc:
        return 1, "", str(exc)


def load_setup_info():
    info = {}
    try:
        with open("/root/.setup_info") as handle:
            for line in handle:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    key, value = line.split("=", 1)
                    info[key] = value
    except FileNotFoundError:
        pass
    return info


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def decode_jwt(token):
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload.encode()).decode())
    except Exception:
        return {}


def dex_login_token(setup_info, username):
    dex_url = setup_info.get("DEX_URL", "http://dex.dex.svc.cluster.local:5556").rstrip("/")
    client_secret = setup_info.get("DEX_CLIENT_SECRET", "")
    redirect_uri = "http://glitchtip.devops.local/accounts/oidc/login/callback/"
    email = f"{username}@devops.local"

    jar = http.cookiejar.CookieJar()
    follow = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    no_redirect = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), NoRedirect)

    query = urllib.parse.urlencode({
        "client_id": "glitchtip",
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid profile email groups",
        "state": f"grader-{int(time.time())}",
    })

    try:
        with follow.open(f"{dex_url}/auth?{query}", timeout=20) as response:
            html = response.read().decode(errors="ignore")
            current_url = response.geturl()
    except Exception as exc:
        return None, f"Dex auth bootstrap failed for {username}: {exc}"

    match = re.search(r'<form[^>]+action="([^"]+)"', html)
    if not match:
        return None, f"Dex login form not found for {username}; url={current_url}"

    post_url = urllib.parse.urljoin(current_url, match.group(1).replace("&amp;", "&"))
    request = urllib.request.Request(
        post_url,
        data=urllib.parse.urlencode({"login": email, "password": USER_PASSWORD}).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )

    for _ in range(8):
        try:
            no_redirect.open(request, timeout=20)
            return None, f"Dex login for {username} unexpectedly did not redirect"
        except urllib.error.HTTPError as exc:
            if exc.code not in (302, 303):
                body = exc.read(300).decode(errors="ignore")
                return None, f"Dex login failed for {username}: HTTP {exc.code} {body}"
            location = exc.headers.get("Location", "")
        except Exception as exc:
            return None, f"Dex login request failed for {username}: {exc}"

        if not location:
            return None, f"Dex login redirect missing Location for {username}"

        if location.startswith(redirect_uri):
            params = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
            code = (params.get("code") or [""])[0]
            if not code:
                return None, f"Dex callback for {username} did not include an auth code"

            token_request = urllib.request.Request(
                f"{dex_url}/token",
                data=urllib.parse.urlencode({
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                }).encode(),
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Authorization": "Basic " + base64.b64encode(
                        f"glitchtip:{client_secret}".encode()
                    ).decode(),
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(token_request, timeout=20) as token_response:
                    return json.loads(token_response.read().decode()), ""
            except Exception as exc:
                return None, f"Dex token exchange failed for {username}: {exc}"

        request = urllib.request.Request(urllib.parse.urljoin(dex_url, location), method="GET")

    return None, f"Dex login redirect loop did not reach callback for {username}"


def get_pod(namespace, selector):
    rc, out, _ = run_cmd(
        f"kubectl get pods -n {namespace} -l {shlex.quote(selector)} "
        "-o jsonpath='{.items[0].metadata.name}' 2>/dev/null",
        timeout=20,
    )
    return out.strip("'") if rc == 0 and out.strip("'") else ""


def get_pg_pod():
    return get_pod("glitchtip", "app.kubernetes.io/name=postgresql")


def pg_query(sql, setup_info, timeout=20):
    pod = get_pg_pod()
    if not pod:
        return 1, "", "PostgreSQL pod not found"
    password = setup_info.get("GT_DB_PASS", "7KkJeWZYkK")
    user = setup_info.get("GT_DB_USER", "postgres")
    db = setup_info.get("GT_DB_NAME", "postgres")
    escaped = sql.replace('"', '\\"')
    return run_cmd(
        f"kubectl exec -n glitchtip {pod} -- bash -c "
        f"\"PGPASSWORD={password} psql -U {user} -d {db} -tAc \\\"{escaped}\\\"\"",
        timeout=timeout,
    )


def get_user_roles(setup_info):
    sql = """
    SELECT u.email || '=' || ou.role
    FROM organizations_ext_organizationuser ou
    JOIN users_user u ON u.id = ou.user_id
    JOIN organizations_ext_organization o ON o.id = ou.organization_id
    WHERE o.slug = 'devops-platform'
    ORDER BY u.email;
    """
    rc, out, err = pg_query(" ".join(sql.split()), setup_info)
    if rc != 0:
        return None, err or out
    roles = {}
    for line in out.splitlines():
        if "=" not in line:
            continue
        email, role = line.strip().split("=", 1)
        try:
            roles[email] = int(role)
        except ValueError:
            roles[email] = role
    return roles, ""


def get_ldap_owner_members(setup_info):
    ldap_pod = get_pod("ldap", "app=openldap")
    if not ldap_pod:
        return None, "LDAP pod not found"
    base_dn = setup_info.get("LDAP_BASE_DN", "dc=devops,dc=local")
    admin_dn = setup_info.get("LDAP_ADMIN_DN", "cn=admin,dc=devops,dc=local")
    admin_pw = setup_info.get("LDAP_ADMIN_PASSWORD", "ldap-admin-2026")
    cmd = (
        f"kubectl exec -n ldap {ldap_pod} -- ldapsearch -x "
        f"-D {shlex.quote(admin_dn)} -w {shlex.quote(admin_pw)} "
        f"-b {shlex.quote('cn=glitchtip-owners,ou=groups,' + base_dn)} uniqueMember"
    )
    rc, out, err = run_cmd(cmd, timeout=20)
    if rc != 0:
        return None, err or out
    members = []
    for match in re.findall(r"uid=([^,]+),ou=people", out):
        if match not in members:
            members.append(match)
    return sorted(members), ""


def dex_config_uses_ldap():
    rc, out, err = run_cmd(
        "kubectl get configmap dex-config -n dex "
        "-o go-template='{{ index .data \"config.yaml\" }}' 2>/dev/null",
        timeout=20,
    )
    if rc != 0 or not out:
        return False, err or "Dex config not found"
    lowered = out.lower()
    required = ["type: ldap", "openldap.ldap.svc", "groupsearch", "uniquemember"]
    missing = [token for token in required if token not in lowered]
    if missing:
        return False, f"Dex config missing LDAP markers: {missing}"
    return True, "Dex config uses the LDAP connector and group search"


def redis_cmd(*args, timeout=20):
    redis_pod = get_pod("glitchtip", "app=glitchtip-auth-replay-cache")
    if not redis_pod:
        return 1, "", "Redis replay cache pod not found"
    quoted = " ".join(shlex.quote(str(arg)) for arg in args)
    return run_cmd(
        f"kubectl exec -n glitchtip {redis_pod} -- redis-cli {quoted}",
        timeout=timeout,
    )


def redis_get(key):
    rc, out, _ = redis_cmd("GET", key)
    if rc != 0:
        return ""
    return out.strip()


def redis_smembers(key):
    rc, out, err = redis_cmd("SMEMBERS", key)
    if rc != 0:
        return None, err or out
    return sorted([line.strip() for line in out.splitlines() if line.strip()])


def get_bootstrap_snapshot():
    rc, out, _ = run_cmd(
        "kubectl get configmap glitchtip-auth-bootstrap-snapshot -n glitchtip "
        "-o go-template='{{ index .data \"owner-replay.txt\" }}' 2>/dev/null",
        timeout=20,
    )
    if rc != 0:
        return []
    return sorted([line.strip() for line in out.splitlines() if line.strip()])


def wait_for_job_result(job_name, timeout_seconds=120):
    for _ in range(max(1, int(timeout_seconds / 5))):
        rc, status, _ = run_cmd(
            f"kubectl get job {job_name} -n glitchtip "
            "-o jsonpath='{.status.succeeded}/{.status.failed}' 2>/dev/null",
            timeout=15,
        )
        parts = status.strip("'").split("/")
        succeeded = parts[0] if parts else ""
        failed = parts[1] if len(parts) > 1 else ""
        if succeeded == "1":
            return True, "completed"
        if failed.isdigit() and int(failed) > 0:
            return False, "failed"
        time.sleep(5)
    return False, "timeout"


def force_replay_cycle():
    rc, _, _ = run_cmd("kubectl get cronjob glitchtip-auth-session-warm -n glitchtip >/dev/null 2>&1", timeout=15)
    if rc != 0:
        return True, "Replay CronJob removed"

    job_name = f"grader-auth-replay-{int(time.time())}"
    run_cmd(f"kubectl delete job {job_name} -n glitchtip --ignore-not-found=true >/dev/null 2>&1", timeout=15)
    rc, out, err = run_cmd(
        f"kubectl create job {job_name} --from=cronjob/glitchtip-auth-session-warm -n glitchtip",
        timeout=20,
    )
    if rc != 0:
        return False, f"Could not create replay job: {(err or out)[:200]}"
    completed, detail = wait_for_job_result(job_name, timeout_seconds=120)
    run_cmd(f"kubectl delete job {job_name} -n glitchtip --ignore-not-found=true >/dev/null 2>&1", timeout=20)
    return completed, f"Forced replay job {detail}"


def check_identity_replay_alignment(setup_info):
    dex_ok, dex_msg = dex_config_uses_ldap()
    if not dex_ok:
        return 0.0, dex_msg
    members, err = get_ldap_owner_members(setup_info)
    if members is None:
        return 0.0, err
    if members != OWNER_USERS:
        return 0.0, f"LDAP owners are {members}, expected {OWNER_USERS}"
    snapshot = get_bootstrap_snapshot()
    stale_in_snapshot = [user for user in MEMBER_USERS if user in snapshot]
    if stale_in_snapshot:
        return 0.0, (
            f"Dex/LDAP truth is correct, but replay bootstrap still contradicts it: "
            f"stale users={stale_in_snapshot}; snapshot={snapshot}"
        )
    if snapshot and snapshot != OWNER_USERS:
        return 0.0, f"Replay bootstrap does not match LDAP owners. LDAP={members}; snapshot={snapshot}"
    return 1.0, f"{dex_msg}; LDAP owners and replay bootstrap align: {members}"


def check_existing_glitchtip_roles_correct(setup_info):
    roles, err = get_user_roles(setup_info)
    if roles is None:
        return 0.0, err
    failures = []
    for username in OWNER_USERS:
        role = roles.get(f"{username}@devops.local")
        if role != 3:
            failures.append(f"{username} role={role}, expected owner role 3")
    for username in MEMBER_USERS:
        role = roles.get(f"{username}@devops.local")
        if role == 3:
            failures.append(f"{username} is still owner")
        if role is None:
            failures.append(f"{username} missing from organization")
    if failures:
        return 0.0, "; ".join(failures)
    return 1.0, f"GlitchTip roles corrected: {roles}"


def check_fresh_sso_claims_match_runtime(setup_info):
    roles, err = get_user_roles(setup_info)
    if roles is None:
        return 0.0, err

    owner = OWNER_USERS[0]
    member = MEMBER_USERS[0]

    owner_tokens, owner_err = dex_login_token(setup_info, owner)
    if not owner_tokens:
        return 0.0, owner_err
    member_tokens, member_err = dex_login_token(setup_info, member)
    if not member_tokens:
        return 0.0, member_err

    owner_claims = decode_jwt(owner_tokens.get("id_token") or owner_tokens.get("access_token", ""))
    member_claims = decode_jwt(member_tokens.get("id_token") or member_tokens.get("access_token", ""))
    owner_groups = owner_claims.get("groups") or []
    member_groups = member_claims.get("groups") or []

    if "glitchtip-owners" not in owner_groups:
        return 0.0, f"{owner} fresh Dex/LDAP login lacks owner group. Groups={owner_groups}"
    if "glitchtip-owners" in member_groups:
        return 0.0, f"{member} fresh Dex/LDAP login still has owner group. Groups={member_groups}"
    if "glitchtip-users" not in member_groups:
        return 0.0, f"{member} fresh Dex/LDAP login lacks normal user group. Groups={member_groups}"

    owner_role = roles.get(f"{owner}@devops.local")
    member_role = roles.get(f"{member}@devops.local")
    if owner_role != 3:
        return 0.0, f"{owner} has correct Dex claims but GlitchTip runtime role={owner_role}"
    if member_role == 3:
        return 0.0, f"{member} has non-owner Dex claims but is still owner in GlitchTip runtime"

    return 1.0, (
        f"Fresh Dex/LDAP claims match runtime roles. "
        f"{owner} groups={owner_groups}, role={owner_role}; "
        f"{member} groups={member_groups}, role={member_role}"
    )


def check_redis_replay_source_clean(setup_info):
    org_slug = setup_info.get("ORG_SLUG", "devops-platform")
    replay_members, err = redis_smembers(f"authz:glitchtip:{org_slug}:owner-replay")
    if replay_members is None:
        return 0.0, err

    stale_in_redis = [user for user in MEMBER_USERS if user in replay_members]
    snapshot = get_bootstrap_snapshot()
    stale_in_snapshot = [user for user in MEMBER_USERS if user in snapshot]
    if stale_in_redis or stale_in_snapshot:
        return 0.0, (
            f"Stale replay source remains. Redis stale={stale_in_redis}; "
            f"snapshot stale={stale_in_snapshot}; redis={replay_members}; snapshot={snapshot}"
        )
    for username in OWNER_USERS:
        if username not in replay_members and snapshot and username not in snapshot:
            return 0.0, f"Owner {username} missing from replay source"
    return 1.0, f"Replay source clean. Redis={replay_members}; snapshot={snapshot or 'absent'}"


def check_stale_sessions_invalidated(setup_info):
    stale = []
    for username in MEMBER_USERS:
        for prefix in ("authz:session", "authz:effective"):
            key = f"{prefix}:{username}:role"
            value = redis_get(key)
            if value.lower() == "owner":
                stale.append(key)
    if stale:
        return 0.0, f"Stale owner session/effective-role keys still present: {stale}"

    missing_owner_cache = []
    for username in OWNER_USERS:
        session = redis_get(f"authz:session:{username}:role")
        effective = redis_get(f"authz:effective:{username}:role")
        if session and session.lower() != "owner":
            missing_owner_cache.append(f"{username} session={session}")
        if effective and effective.lower() != "owner":
            missing_owner_cache.append(f"{username} effective={effective}")
    if missing_owner_cache:
        return 0.0, "; ".join(missing_owner_cache)
    return 1.0, "Non-admin stale privileged session/effective-role keys are invalidated"


def check_replay_cycle_durable(setup_info):
    ok, detail = force_replay_cycle()
    if not ok:
        return 0.0, detail

    print("[grader] Waiting 120s durability window after replay cycle...")
    time.sleep(120)

    role_score, role_feedback = check_existing_glitchtip_roles_correct(setup_info)
    if role_score == 0:
        return 0.0, f"Roles drifted after replay cycle: {role_feedback}"

    source_score, source_feedback = check_redis_replay_source_clean(setup_info)
    if source_score == 0:
        return 0.0, f"Replay source drifted after replay cycle: {source_feedback}"

    session_score, session_feedback = check_stale_sessions_invalidated(setup_info)
    if session_score == 0:
        return 0.0, f"Sessions drifted after replay cycle: {session_feedback}"

    return 1.0, f"{detail}; roles/source/session state remained safe"


def grade(*args, **kwargs):
    os.environ["KUBECONFIG"] = "/etc/rancher/k3s/k3s.yaml"
    setup_info = load_setup_info()

    run_cmd("pkill -u ubuntu 2>/dev/null || true", timeout=10)

    checks = {
        "identity_replay_alignment": check_identity_replay_alignment,
        "existing_glitchtip_roles_correct": check_existing_glitchtip_roles_correct,
        "fresh_sso_claims_match_runtime": check_fresh_sso_claims_match_runtime,
        "redis_replay_source_clean": check_redis_replay_source_clean,
        "stale_sessions_invalidated": check_stale_sessions_invalidated,
        "replay_cycle_durable": check_replay_cycle_durable,
    }
    weight = 1.0 / len(checks)
    subscores = {}
    weights = {}
    feedback = []

    for name, fn in checks.items():
        try:
            score, detail = fn(setup_info)
        except Exception as exc:
            score, detail = 0.0, f"Exception: {exc}"
        subscores[name] = score
        weights[name] = weight
        feedback.append(f"[{name}] {'PASS' if score else 'FAIL'}: {detail}")
        print(f"[grader] {name}: {score} - {detail}")

    total = sum(subscores[name] * weights[name] for name in subscores)
    print(f"[grader] Final score: {total:.4f}")
    return GradingResult(score=total, subscores=subscores, weights=weights, feedback="\n".join(feedback))


if __name__ == "__main__":
    result = grade()
    print(json.dumps({
        "score": result.score,
        "subscores": result.subscores,
        "weights": result.weights,
        "feedback": result.feedback,
    }, indent=2))
