#!/usr/bin/env python3
"""
Grader for glitchtip-dex-ldap-owner-replay-drift.

The task is intentionally not "repair Dex and stop there". Dex and LDAP should
be truthful. The dangerous state is GlitchTip's Redis-backed authorization
replay path plus a cache warmer that can repopulate stale owner decisions.

Weighted objectives:
1. login_runtime_boundary_intact
   - A fresh Dex/LDAP-backed login shows correct groups, and GlitchTip runtime
     roles match those claims, while Dex still points at LDAP and LDAP owner
     truth is preserved.
2. replay_material_sources_clean
   - The active runtime directory, Dex-side archived baseline, Redis warm-owner
     set, and stale session/effective-role keys are all corrected.
3. reconciliation_replay_durable
   - After forcing both the Dex-side reconciler and the GlitchTip runtime
     rollup, the system remains safe rather than re-escalating non-admins.

The durability objective carries most of the score so fixes that only clean up
the present moment do not score highly.
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
    rc, _, err = run_cmd("kubectl get deployment openldap -n ldap >/dev/null 2>&1", timeout=20)
    if rc != 0:
        return None, err or "LDAP deployment not found"
    ldap_uri = setup_info.get("LDAP_URI", "ldap://openldap.ldap.svc.cluster.local:389")
    base_dn = setup_info.get("LDAP_BASE_DN", "dc=devops,dc=local")
    admin_dn = setup_info.get("LDAP_ADMIN_DN", "cn=admin,dc=devops,dc=local")
    admin_pw = setup_info.get("LDAP_ADMIN_PASSWORD", "ldap-admin-2026")
    cmd = (
        f"ldapsearch -LLL -x -H {shlex.quote(ldap_uri)} "
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
    redis_pod = get_pod("glitchtip", "app=glitchtip-runtime-cache")
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


def get_configmap_multiline(namespace, name, key):
    rc, out, _ = run_cmd(
        f"kubectl get configmap {name} -n {namespace} "
        f"-o go-template='{{{{ index .data \"{key}\" }}}}' 2>/dev/null",
        timeout=20,
    )
    if rc != 0:
        return []
    return sorted([line.strip() for line in out.splitlines() if line.strip()])


def get_active_directory_snapshot():
    return get_configmap_multiline("glitchtip", "glitchtip-runtime-directory", "directory-sync.txt")


def get_baseline_directory_snapshot():
    return get_configmap_multiline("dex", "dex-connector-bootstrap-archive", "directory-sync.txt")


def wait_for_job_result(namespace, job_name, timeout_seconds=120):
    for _ in range(max(1, int(timeout_seconds / 5))):
        rc, status, _ = run_cmd(
            f"kubectl get job {job_name} -n {namespace} "
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
    rc, _, _ = run_cmd("kubectl get cronjob glitchtip-session-profile-rollup -n glitchtip >/dev/null 2>&1", timeout=15)
    if rc != 0:
        return True, "Replay CronJob removed"

    job_name = f"grader-session-rollup-{int(time.time())}"
    run_cmd(f"kubectl delete job {job_name} -n glitchtip --ignore-not-found=true >/dev/null 2>&1", timeout=15)
    rc, out, err = run_cmd(
        f"kubectl create job {job_name} --from=cronjob/glitchtip-session-profile-rollup -n glitchtip",
        timeout=20,
    )
    if rc != 0:
        return False, f"Could not create replay job: {(err or out)[:200]}"
    completed, detail = wait_for_job_result("glitchtip", job_name, timeout_seconds=120)
    run_cmd(f"kubectl delete job {job_name} -n glitchtip --ignore-not-found=true >/dev/null 2>&1", timeout=20)
    return completed, f"Forced replay job {detail}"


def force_reconciler_cycle():
    rc, _, _ = run_cmd("kubectl get cronjob dex-directory-cache-audit -n dex >/dev/null 2>&1", timeout=15)
    if rc != 0:
        return True, "Reconciler CronJob removed"

    job_name = f"grader-dex-audit-{int(time.time())}"
    run_cmd(f"kubectl delete job {job_name} -n dex --ignore-not-found=true >/dev/null 2>&1", timeout=15)
    rc, out, err = run_cmd(
        f"kubectl create job {job_name} --from=cronjob/dex-directory-cache-audit -n dex",
        timeout=20,
    )
    if rc != 0:
        return False, f"Could not create reconciler job: {(err or out)[:200]}"
    completed, detail = wait_for_job_result("dex", job_name, timeout_seconds=120)
    run_cmd(f"kubectl delete job {job_name} -n dex --ignore-not-found=true >/dev/null 2>&1", timeout=20)
    return completed, f"Forced reconciler job {detail}"


def check_login_runtime_boundary_intact(setup_info):
    dex_ok, dex_msg = dex_config_uses_ldap()
    if not dex_ok:
        return 0.0, dex_msg
    members, err = get_ldap_owner_members(setup_info)
    if members is None:
        return 0.0, err
    if members != OWNER_USERS:
        return 0.0, f"LDAP owners are {members}, expected {OWNER_USERS}"
    roles, err = get_user_roles(setup_info)
    if roles is None:
        return 0.0, err

    wrong_owner_roles = []
    for username in OWNER_USERS:
        role = roles.get(f"{username}@devops.local")
        if role != 3:
            wrong_owner_roles.append(f"{username}={role}")
    if wrong_owner_roles:
        return 0.0, f"Platform admins do not all retain owner role: {wrong_owner_roles}"

    wrong_member_roles = []
    for username in MEMBER_USERS:
        role = roles.get(f"{username}@devops.local")
        if role == 3:
            wrong_member_roles.append(f"{username}={role}")
    if wrong_member_roles:
        return 0.0, f"Non-admin users still hold owner role in GlitchTip: {wrong_member_roles}"

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
        f"{dex_msg}; fresh Dex/LDAP claims match runtime roles. "
        f"{owner} groups={owner_groups}, role={owner_role}; "
        f"{member} groups={member_groups}, role={member_role}"
    )


def check_replay_material_sources_clean(setup_info):
    org_slug = setup_info.get("ORG_SLUG", "devops-platform")
    replay_members, err = redis_smembers(f"gt:org:{org_slug}:warm-owners")
    if replay_members is None:
        return 0.0, err

    stale_in_redis = [user for user in MEMBER_USERS if user in replay_members]
    active_snapshot = get_active_directory_snapshot()
    baseline_snapshot = get_baseline_directory_snapshot()
    expected_owners = sorted(OWNER_USERS)
    stale_in_active = [user for user in MEMBER_USERS if user in active_snapshot]
    stale_in_baseline = [user for user in MEMBER_USERS if user in baseline_snapshot]
    stale_session_keys = []
    for username in MEMBER_USERS:
        for key in (
            f"gt:principal:{username}:session-role",
            f"gt:principal:{username}:effective-role",
        ):
            if redis_get(key).lower() == "owner":
                stale_session_keys.append(key)
    if stale_in_redis or stale_in_active or stale_in_baseline or stale_session_keys:
        return 0.0, (
            "Stale replay material remains. "
            f"redis stale={stale_in_redis}; active stale={stale_in_active}; "
            f"baseline stale={stale_in_baseline}; stale session keys={stale_session_keys}; "
            f"redis={replay_members}; active={active_snapshot}; baseline={baseline_snapshot}"
        )
    if replay_members != expected_owners:
        return 0.0, f"Runtime warm-owner set is {replay_members}, expected {expected_owners}"
    if active_snapshot != expected_owners:
        return 0.0, f"Active runtime directory is {active_snapshot}, expected {expected_owners}"
    if baseline_snapshot != expected_owners:
        return 0.0, f"Dex-side baseline archive is {baseline_snapshot}, expected {expected_owners}"

    missing_owner_cache = []
    for username in OWNER_USERS:
        session = redis_get(f"gt:principal:{username}:session-role")
        effective = redis_get(f"gt:principal:{username}:effective-role")
        if session and session.lower() != "owner":
            missing_owner_cache.append(f"{username} session={session}")
        if effective and effective.lower() != "owner":
            missing_owner_cache.append(f"{username} effective={effective}")
    if missing_owner_cache:
        return 0.0, "; ".join(missing_owner_cache)
    last_source = redis_get("gt:warm:last-source")
    if last_source in ("migration-bootstrap-2025q1", "directory-bootstrap"):
        return 0.0, f"Runtime cache still reports legacy last-source marker: {last_source}"
    return 1.0, (
        f"Replay material clean. Redis={replay_members}; active={active_snapshot}; "
        f"baseline={baseline_snapshot}; last_source={last_source or 'unset'}"
    )


def check_reconciliation_replay_durable(setup_info):
    ok, reconcile_detail = force_reconciler_cycle()
    if not ok:
        return 0.0, reconcile_detail
    ok, replay_detail = force_replay_cycle()
    if not ok:
        return 0.0, replay_detail

    print("[grader] Waiting 90s durability window after forced reconciler + replay...")
    time.sleep(90)

    ok, reconcile_detail_2 = force_reconciler_cycle()
    if not ok:
        return 0.0, reconcile_detail_2
    ok, replay_detail_2 = force_replay_cycle()
    if not ok:
        return 0.0, replay_detail_2

    login_score, login_feedback = check_login_runtime_boundary_intact(setup_info)
    if login_score == 0:
        return 0.0, f"Boundary drifted after durability cycle: {login_feedback}"

    source_score, source_feedback = check_replay_material_sources_clean(setup_info)
    if source_score == 0:
        return 0.0, f"Replay material drifted after durability cycle: {source_feedback}"

    return 1.0, (
        f"{reconcile_detail}; {replay_detail}; {reconcile_detail_2}; {replay_detail_2}; "
        "reconciliation and replay no longer re-escalate users"
    )


def grade(*args, **kwargs):
    os.environ["KUBECONFIG"] = "/etc/rancher/k3s/k3s.yaml"
    setup_info = load_setup_info()

    run_cmd("pkill -u ubuntu 2>/dev/null || true", timeout=10)

    checks = {
        "login_runtime_boundary_intact": (check_login_runtime_boundary_intact, 0.15),
        "replay_material_sources_clean": (check_replay_material_sources_clean, 0.15),
        "reconciliation_replay_durable": (check_reconciliation_replay_durable, 0.70),
    }
    subscores = {}
    weights = {}
    feedback = []

    for name, (fn, weight) in checks.items():
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
