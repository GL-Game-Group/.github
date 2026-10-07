#!/usr/bin/env python3
"""glwork deploy helper used by .github/workflows/deploy.yml and release-bot.

Reads deploy.yml (all apps of a repository, test and production settings) from the
application repository, validates it against the platform rules and renders what the
workflow needs. Which apps a push deploys is decided by the tags on the pushed commit:
"<app>@<version>", or "<app>" alone (the next version is assigned automatically); several apps
can share one tag separated by commas ("web@1.2,api@2.0" or "web,api").

  glwork_deploy.py plan                                 -> GITHUB_OUTPUT: apps to deploy ($DEPLOY_ENV, $TAGS)
  glwork_deploy.py render-k8s ENV APP IMAGE VERSION     -> Kubernetes manifests on stdout
  glwork_deploy.py needs-approval ENV APP               -> "true" / "false" from the approval policy
  glwork_deploy.py approval-message APP VERSION [ENV]   -> Telegram sendMessage JSON on stdout (ENV default prod)
  glwork_deploy.py promote-k8s INFRA_DIR APP IMAGE VERSION -> write the production bundle into infra
  glwork_deploy.py wrangler-static ENV APP ASSETS_DIR    -> wrangler config of a static site (type: static)
  glwork_deploy.py image-exists IMAGE:TAG               -> exit 0 if the tag is already in the registry
  glwork_deploy.py telegram TEXT                        -> Telegram sendMessage JSON for a notice
  glwork_deploy.py ingress-conflict HOST NS NAME < ingresses.json -> exit 1 (reason on stdout) if
                                                           another Ingress already serves HOST

Only the standard library is used: deploy.yml is read by a small YAML subset parser
(nested "key: value" mappings, inline {a: b} maps, '#' comments; no lists or anchors).
"""
import base64
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

FRPS_IP = "8.217.141.116"
# Deploy targets: Rancher clusters labelled glwork.net/target=<name>. The platform team adds a
# cluster here when it joins Rancher. ingress: the IP DNS records point at; proxied: Cloudflare proxy.
DEFAULT_TARGET = "office"
TARGETS = {
    "office": {"ingress": FRPS_IP, "proxied": False, "direct": True},  # office cluster, via the HK frps
}
REGISTRY = "glwork-registry.cn-hongkong.cr.aliyuncs.com/glwork"
DESCRIPTOR = "deploy.yml"
NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,38}[a-z0-9])?$")
VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,40}$")
NUMERIC_VERSION_RE = re.compile(r"^\d+(\.\d+)*$")
TEST_HOST_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?\.(int\.)?glwork\.dev$")
PROD_HOST_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?\.glwork\.net$")
# Static sites are served by Cloudflare, so int.glwork.dev (office-only) does not apply.
TEST_STATIC_HOST_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?\.glwork\.dev$")
FIELDS = {"type", "port", "health", "replicas", "cpu", "memory", "memory_limit", "env_secret", "context",
          "dockerfile", "public", "team", "target", "host", "namespace", "wrangler_env", "workdir", "url",
          "database", "build", "output", "spa"}
ENVS = ("test", "prod")
# Hosts that belong to the platform (not deployable by apps); other clusters' hosts are checked live.
RESERVED_HOSTS = {"rancher.glwork.net": "Rancher 管理平台", "secrets.glwork.net": "配置入口"}


def fail(msg):
    print(f"::error::{msg}", file=sys.stderr)
    sys.exit(2)


# --- deploy.yml -------------------------------------------------------------
def parse_yaml(text, path=DESCRIPTOR):
    """Nested block mappings, inline {k: v} maps and scalars; everything else is an error."""
    def scalar(v):
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            return v[1:-1]
        return v

    root = {}
    stack = [(-1, root)]
    for n, raw in enumerate(text.splitlines(), 1):
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            fail(f"{path}:{n}: use spaces, not tabs, for indentation")
        line = re.sub(r"(^|\s)#.*$", "", raw).rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        body = line.strip()
        if body.startswith("- "):
            fail(f"{path}:{n}: lists are not supported: {raw.strip()}")
        m = re.match(r"^([A-Za-z0-9_.-]+)\s*:(?:\s+(.*))?$", body)
        if not m:
            fail(f"{path}:{n}: expected 'key: value': {raw.strip()}")
        key, val = m.group(1), (m.group(2) or "").strip()
        while stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1]
        if key in parent:
            fail(f"{path}:{n}: duplicate key '{key}'")
        if val == "":
            parent[key] = {}
            stack.append((indent, parent[key]))
        elif val.startswith("{"):
            if not val.endswith("}"):
                fail(f"{path}:{n}: inline map must end with '}}' on the same line")
            inner, d = val[1:-1].strip(), {}
            for part in filter(None, (p.strip() for p in inner.split(","))):
                km = re.match(r"^([A-Za-z0-9_.-]+)\s*:\s*(.*)$", part)
                if not km or km.group(2).strip().startswith(("{", "[")):
                    fail(f"{path}:{n}: inline map entries must be 'key: value': {part}")
                d[km.group(1)] = scalar(km.group(2))
            parent[key] = d
        elif val.startswith("["):
            fail(f"{path}:{n}: lists are not supported: {raw.strip()}")
        else:
            parent[key] = scalar(val)
    return root


def load():
    if not os.path.exists(DESCRIPTOR):
        return None
    d = parse_yaml(open(DESCRIPTOR, encoding="utf-8").read())
    for k in d:
        if k not in ("defaults", "apps"):
            fail(f"{DESCRIPTOR}: unknown top-level key '{k}' (expected defaults, apps)")
    apps = d.get("apps")
    if not isinstance(apps, dict) or not apps:
        fail(f"{DESCRIPTOR}: 'apps' must list at least one app")
    defaults = d.get("defaults", {})
    check_fields(defaults, "defaults", envs=False)
    for name, app in apps.items():
        if not NAME_RE.match(name):
            fail(f"{DESCRIPTOR}: app name '{name}' must be lowercase letters, digits and '-' (max 40)")
        if not isinstance(app, dict):
            fail(f"{DESCRIPTOR}: apps.{name} must be a mapping")
        check_fields(app, f"apps.{name}", envs=True)
    return {"defaults": defaults, "apps": apps}


def check_fields(d, where, envs):
    if not isinstance(d, dict):
        fail(f"{DESCRIPTOR}: {where} must be a mapping")
    for k, v in d.items():
        if envs and k in ENVS:
            if not isinstance(v, dict):
                fail(f"{DESCRIPTOR}: {where}.{k} must be a mapping (use {{}} for defaults only)")
            check_fields(v, f"{where}.{k}", envs=False)
        elif k not in FIELDS:
            fail(f"{DESCRIPTOR}: unknown field '{where}.{k}'")
        elif isinstance(v, dict):
            fail(f"{DESCRIPTOR}: {where}.{k} must be a value, not a mapping")


def settings(doc, app, env):
    """Merged settings of one app in one environment, or None when the app has no such section."""
    a = doc["apps"].get(app)
    if a is None or env not in a:
        return None
    d = {**doc["defaults"], **{k: v for k, v in a.items() if k not in ENVS}, **a[env]}
    where = f"{DESCRIPTOR} apps.{app}.{env}"
    s = {"env": env, "name": app, "type": d.get("type", "k8s")}
    if s["type"] not in ("k8s", "worker", "static"):
        fail(f"{where}: type must be k8s, worker or static")
    database = d.get("database", "")
    if database not in ("", "postgres"):
        fail(f"{where}: database must be postgres")
    if s["type"] == "static":
        # Assets-only Cloudflare Worker; the pipeline writes its wrangler config (wrangler-static).
        if database:
            fail(f"{where}: database is only for k8s apps (static sites run on Cloudflare)")
        s["workdir"] = d.get("workdir", ".")
        s["build"] = d.get("build", "")
        s["output"] = d.get("output", "dist")
        s["spa"] = d.get("spa", "false") == "true"
        s["public"] = True
        s["host"] = d.get("host", f"{app}.glwork.{'dev' if env == 'test' else 'net'}")
        host_re = TEST_STATIC_HOST_RE if env == "test" else PROD_HOST_RE
        if not host_re.match(s["host"]):
            fail(f"{where}: host '{s['host']}' must be <name>.glwork.{'dev' if env == 'test' else 'net'}")
        s["url"] = f"https://{s['host']}"
        s["worker"] = f"{app}-test" if env == "test" else app
        s["target"] = "cloudflare"
        return s
    if s["type"] == "worker":
        if database:
            fail(f"{where}: database is only for k8s apps (Workers cannot reach the office database)")
        s["wrangler_env"] = d.get("wrangler_env", "test" if env == "test" else "production")
        s["url"] = d.get("url", "")
        s["workdir"] = d.get("workdir", ".")
        s["target"] = f"worker:{s['wrangler_env']}"
        return s
    s["port"] = d.get("port", "8080")
    if not s["port"].isdigit():
        fail(f"{where}: port must be a number")
    s["health"] = d.get("health", "/")
    s["replicas"] = d.get("replicas", "1" if env == "test" else "2")
    s["cpu"] = d.get("cpu", "50m")
    s["memory"] = d.get("memory", "64Mi")
    s["memory_limit"] = d.get("memory_limit", "256Mi")
    s["env_secret"] = d.get("env_secret", "")
    s["database"] = database
    s["context"] = d.get("context", ".")
    s["dockerfile"] = d.get("dockerfile", "")
    s["public"] = d.get("public", "true") == "true"
    s["target"] = d.get("target", DEFAULT_TARGET)
    if s["target"] not in TARGETS:
        fail(f"{where}: target '{s['target']}' is not a registered cluster ({', '.join(TARGETS)}); ask the platform team")
    s["direct"] = bool(TARGETS[s["target"]].get("direct"))  # the build machines can reach the cluster
    if env == "test":
        s["team"] = d.get("team", "")
        if not NAME_RE.match(s["team"] or "-"):
            fail(f"{where}: team is required (the namespace assigned by the platform)")
        if not TARGETS[s["target"]].get("direct"):
            fail(f"{where}: test deployments currently run on target {DEFAULT_TARGET} only")
        s["namespace"] = s["team"]
        s["host"] = d.get("host", f"{app}.glwork.dev")
        if s["public"] and not TEST_HOST_RE.match(s["host"]):
            fail(f"{where}: host '{s['host']}' must be <name>.glwork.dev or <name>.int.glwork.dev")
    else:
        s["namespace"] = d.get("namespace", app)
        s["host"] = d.get("host", f"{app}.glwork.net")
        if s["public"] and not PROD_HOST_RE.match(s["host"]):
            fail(f"{where}: host '{s['host']}' must be <name>.glwork.net")
    return s


def host_problems(doc, env):
    """Domain conflicts inside deploy.yml: {app: reason} (same host twice, or a platform host)."""
    hosts, problems = {}, {}
    for app in doc["apps"]:
        s = settings(doc, app, env)
        if s and s["type"] in ("k8s", "static") and s["public"]:
            hosts.setdefault(s["host"], []).append(app)
    for host, apps in hosts.items():
        if host in RESERVED_HOSTS:
            for a in apps:
                problems[a] = f"域名 {host} 是平台保留域名（{RESERVED_HOSTS[host]}）"
        elif len(apps) > 1:
            for a in apps:
                problems[a] = f"域名 {host} 在 {DESCRIPTOR} 中被多个应用使用：{', '.join(apps)}"
    return problems


def approval_policy():
    """Approval switches edited in the Rancher extension (mgmt glwork-secrets/glwork-settings).

    $GLWORK_APPROVAL (build machines) or /etc/glwork/approval.json (release-bot); missing: prod only.
    """
    raw = os.environ.get("GLWORK_APPROVAL", "")
    if not raw and os.path.exists("/etc/glwork/approval.json"):
        raw = open("/etc/glwork/approval.json", encoding="utf-8").read()
    try:
        p = json.loads(raw) if raw.strip() else {}
    except ValueError:
        p = {}
    return {"test": bool(p.get("test", False)), "prod": bool(p.get("prod", True)),
            "overrides": p.get("overrides") or []}


def needs_approval(policy, repo, app, env):
    """Most specific rule wins: repo + app, then repo + '*', then the global switch."""
    repo = repo.lower()
    for wanted in (app, "*"):
        for o in policy["overrides"]:
            if str(o.get("repo", "")).lower() == repo and o.get("app", "*") == wanted and o.get(env) is not None:
                return bool(o[env])
    return policy[env]


def cmd_needs_approval(env, app):
    print("true" if needs_approval(approval_policy(), os.environ.get("GITHUB_REPOSITORY", ""), app, env) else "false")


def out(key, value):
    with open(os.environ.get("GITHUB_OUTPUT", "/dev/stdout"), "a") as f:
        f.write(f"{key}={value}\n")


# --- which apps a push deploys ------------------------------------------------
def git_lines(*args):
    r = subprocess.run(["git", *args], capture_output=True, text=True)
    return [l.strip() for l in r.stdout.splitlines() if l.strip()] if r.returncode == 0 else []


def next_version(app, all_tags):
    """Highest numeric version of the app with its last part + 1 (2.0 -> 2.1, 1.4.0 -> 1.4.1); first: 1.0.0."""
    best = None
    for t in all_tags:
        if t.startswith(app + "@") and NUMERIC_VERSION_RE.match(t[len(app) + 1:]):
            v = tuple(int(x) for x in t[len(app) + 1:].split("."))
            best = v if best is None or v > best else best
    return "1.0.0" if best is None else ".".join(map(str, best[:-1] + (best[-1] + 1,)))


def cmd_plan():
    """Resolve the tags on the pushed commit into the apps to deploy.

    $DEPLOY_ENV: test | prod. $TAGS: space-separated tags to use instead of `git tag --points-at HEAD`.
    Outputs: deploy (JSON list of {app, version, tag, create, bare, type}), count, apps, problems.
    """
    env = os.environ.get("DEPLOY_ENV", "")
    if env not in ENVS:
        fail("DEPLOY_ENV must be test or prod")
    doc = load()
    if doc is None:
        out("count", "0")
        out("deploy", "[]")
        out("problems", "")
        print(f"no {DESCRIPTOR}: nothing to deploy")
        return
    names = sorted(doc["apps"])
    out("apps", ", ".join(names))
    head_tags = os.environ.get("TAGS", "").split() or git_lines("tag", "--points-at", "HEAD")
    all_tags = git_lines("tag", "-l")
    deploy, problems, seen = [], [], set()
    conflicts = host_problems(doc, env)
    # "web@1.2,api@2.0" (or "web,api") is shorthand for several app tags: each app gets its own
    # <app>@<version> tag and the combined tag is removed, so per-app history stays in git tag -l.
    parts = [(p.strip(), t) for t in sorted(head_tags) for p in t.split(",") if p.strip()]
    for t, source in parts:
        combined = "," in source
        app, _, version = t.partition("@")
        if app not in doc["apps"]:
            if NAME_RE.match(app):  # looks like an app tag; anything else (v1.2, etc.) is ignored
                problems.append(f"tag {source}: no app '{app}' in {DESCRIPTOR} (apps: {', '.join(names)})")
            continue
        if app in seen:
            problems.append(f"tag {source}: app '{app}' appears more than once on this commit; deployed once")
            continue
        s = settings(doc, app, env)
        if s is None:
            problems.append(f"tag {source}: apps.{app} has no '{env}' section in {DESCRIPTOR}")
            continue
        if app in conflicts:
            problems.append(f"tag {source}: {app} 未部署，{conflicts[app]}")
            continue
        create, bare = False, (source if combined or not version else "")
        if not version:
            existing = [x for x in head_tags if x.startswith(app + "@") and "," not in x]
            if existing:  # the commit already has a version: never give one commit two versions
                version = sorted(existing)[-1].split("@", 1)[1]
            else:
                version, create = next_version(app, all_tags), True
        elif not VERSION_RE.match(version):
            problems.append(f"tag {source}: version '{version}' must be letters, digits, '.', '_' or '-'")
            continue
        elif combined and f"{app}@{version}" not in head_tags:
            if f"{app}@{version}" in all_tags:
                problems.append(f"tag {source}: {app}@{version} already exists on another commit; use a new version")
                continue
            create = True
        seen.add(app)
        deploy.append({"app": app, "version": version, "tag": f"{app}@{version}", "create": create,
                       "bare": bare, "type": s["type"]})
    out("deploy", json.dumps(deploy))
    out("count", str(len(deploy)))
    out("problems", "; ".join(problems))
    print(json.dumps({"env": env, "deploy": deploy, "problems": problems}, ensure_ascii=False, indent=2))


def cmd_settings(env, app):
    """One app's merged settings as JSON (for workflow steps)."""
    s = settings(load() or fail(f"no {DESCRIPTOR}"), app, env)
    if s is None:
        fail(f"apps.{app} has no '{env}' section in {DESCRIPTOR}")
    print(json.dumps(s))


def render(s, image):
    sha = (os.environ.get("DEPLOY_SHA") or os.environ.get("GITHUB_SHA", ""))
    by = os.environ.get("DEPLOYED_BY", os.environ.get("GITHUB_ACTOR", ""))
    via = os.environ.get("DEPLOY_VIA", "ci")
    n = s["name"]
    # Not app.kubernetes.io/managed-by: Fleet (Helm) sets that one, which would show as drift.
    labels = f"{{app.kubernetes.io/name: {n}, glwork.net/managed-by: glwork-deploy}}"
    # database: postgres -> db-provisioner (infra fleet/platform/db-provisioner) creates Secret <app>-db;
    # the pod waits for it, then reads DATABASE_URL / PG* from it.
    refs = [x for x in (s["env_secret"], f"{n}-db" if s.get("database") else "") if x]
    env_from = f"          envFrom: [{', '.join(f'{{secretRef: {{name: {x}}}}}' for x in refs)}]\n" if refs else ""
    dep_labels = labels if not s.get("database") else labels[:-1] + f", glwork.net/database: {s['database']}}}"
    y = f"""apiVersion: apps/v1
kind: Deployment
metadata:
  name: {n}
  namespace: {s['namespace']}
  labels: {dep_labels}
  annotations:
    glwork.net/deployed-by: {json.dumps(by)}
    glwork.net/commit: {json.dumps(sha[:12])}
    glwork.net/version: {json.dumps(s.get('version', ''))}
    glwork.net/via: {json.dumps(via)}
    glwork.net/repository: {json.dumps(os.environ.get('GITHUB_REPOSITORY', ''))}
    kubernetes.io/change-cause: {json.dumps(image + ' by ' + by)}
spec:
  replicas: {s['replicas']}
  revisionHistoryLimit: 10
  selector:
    matchLabels: {{app.kubernetes.io/name: {n}}}
  template:
    metadata:
      labels: {{app.kubernetes.io/name: {n}}}
    spec:
      containers:
        - name: app
          image: {image}
          ports: [{{name: http, containerPort: {s['port']}}}]
{env_from}          readinessProbe:
            httpGet: {{path: {json.dumps(s['health'])}, port: http}}
            periodSeconds: 10
          resources:
            requests: {{cpu: {s['cpu']}, memory: {s['memory']}}}
            limits: {{memory: {s['memory_limit']}}}
---
apiVersion: v1
kind: Service
metadata:
  name: {n}
  namespace: {s['namespace']}
  labels: {labels}
spec:
  selector: {{app.kubernetes.io/name: {n}}}
  ports: [{{name: http, port: 80, targetPort: http}}]
"""
    if s["public"]:
        t = TARGETS[s["target"]]
        proxied, entry = ("true" if t["proxied"] else "false"), t["ingress"]
        ann = f'    external-dns.kubernetes.io/target: "{entry}"\n    external-dns.kubernetes.io/cloudflare-proxied: "{proxied}"\n'
        y += f"""---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: {n}
  namespace: {s['namespace']}
  labels: {labels}
  annotations:
{ann}spec:
  ingressClassName: traefik
  tls: [{{hosts: [{s['host']}]}}]
  rules:
    - host: {s['host']}
      http:
        paths:
          - path: /
            pathType: Prefix
            backend: {{service: {{name: {n}, port: {{name: http}}}}}}
"""
    return y


def cmd_render_k8s(env, app, image, version):
    s = settings(load() or fail(f"no {DESCRIPTOR}"), app, env)
    if not s or s["type"] != "k8s":
        fail(f"apps.{app}.{env} is not a k8s app")
    s["version"] = version
    sys.stdout.write(render(s, image))


def telegram_body(text, buttons=False):
    body = {"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": True}
    if buttons:
        body["reply_markup"] = {"inline_keyboard": [[
            {"text": "✅ 确认", "callback_data": "approve"},
            {"text": "❌ 拒绝", "callback_data": "reject"},
        ]]}
    return json.dumps(body, ensure_ascii=False)


def esc(t):
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def cmd_approval_message(app, version, env="prod"):
    """Message the release-bot acts on. The last line carries the request in machine-readable form."""
    s = settings(load() or fail(f"no {DESCRIPTOR}"), app, env)
    if s is None:
        fail(f"apps.{app} has no '{env}' section in {DESCRIPTOR}")
    repo = os.environ["GITHUB_REPOSITORY"]
    sha = (os.environ.get("DEPLOY_SHA") or os.environ["GITHUB_SHA"])
    run = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{repo}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"
    title = os.environ.get("COMMIT_TITLE", "").strip()[:80]
    worker_version = os.environ.get("WORKER_VERSION", "")
    tested = os.environ.get("TESTED", "")
    lines = [
        f"🟡 <b>待审核</b> · {'正式发布' if env == 'prod' else '测试部署'} → <b>{esc(s['target'])}</b>",
        f"应用：<code>{esc(repo)}</code> · <b>{esc(app)}</b>（{s['type']}）",
        f"版本：<code>{esc(app)}@{esc(version)}</code> · 提交 <code>{sha[:12]}</code> {esc(title)}",
    ]
    if s["type"] == "k8s" and s["public"]:
        lines.append(f"域名：https://{esc(s['host'])}")
    elif s.get("url"):
        lines.append(f"地址：{esc(s['url'])}")
    if tested == "yes":
        lines.append("测试环境：✅ 已构建并部署过此提交")
    elif tested == "no":
        lines.append("测试环境：⚠️ 此提交未在测试环境构建过")
    lines += [
        f"发起人：{esc(os.environ.get('GITHUB_ACTOR', ''))}",
        f"构建：{run}",
    ]
    if worker_version:
        lines.append(f"Worker 版本：<code>{esc(worker_version)}</code>")
    lines.append("群管理员点击下方按钮审核，24 小时内有效。")
    req = {"r": repo, "s": sha, "t": s["target"], "k": s["type"], "a": app, "n": version, "v": worker_version, "e": env}
    lines.append(f"<code>req {esc(json.dumps(req, separators=(',', ':')))}</code>")
    print(telegram_body("\n".join(lines), buttons=True))


def cmd_ingress_conflict(host, namespace, name):
    """Read `kubectl get ingress -A -o json` on stdin; fail if HOST is served by another Ingress."""
    items = json.load(sys.stdin).get("items", [])
    for ing in items:
        m = ing["metadata"]
        if (m["namespace"], m["name"]) == (namespace, name):
            continue
        for rule in ing.get("spec", {}).get("rules", []) or []:
            if rule.get("host") == host:
                print(f"域名 {host} 已被 {m['namespace']}/{m['name']} 使用")
                sys.exit(1)


def cmd_wrangler_static(env, app, assets):
    """wrangler config (JSON) of a static site: assets-only Worker bound to the app's host."""
    s = settings(load() or fail(f"no {DESCRIPTOR}"), app, env)
    if not s or s["type"] != "static":
        fail(f"apps.{app}.{env} is not a static app")
    print(json.dumps({
        "name": s["worker"],
        "compatibility_date": "2025-10-01",
        "workers_dev": False,
        "preview_urls": False,
        "assets": {"directory": assets, "html_handling": "auto-trailing-slash",
                   "not_found_handling": "single-page-application" if s["spa"] else "404-page"},
        "routes": [{"pattern": s["host"], "custom_domain": True}],
    }, indent=2))


def cmd_telegram(text):
    print(telegram_body(text))


def cmd_promote_k8s(infra_dir, app, image, version):
    """Write fleet/apps-prod/<app>/ into the infra checkout (Fleet deploys it to the target cluster)."""
    s = settings(load() or fail(f"no {DESCRIPTOR}"), app, "prod")
    if not s or s["type"] != "k8s":
        fail(f"apps.{app}.prod is not a k8s app")
    s["version"] = version
    d = os.path.join(infra_dir, "fleet", "apps-prod", app)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "fleet.yaml"), "w") as f:
        f.write(f"""# Managed by release-bot: production release of {os.environ.get('GITHUB_REPOSITORY', '')} app {app}@{version}.
defaultNamespace: {s['namespace']}
# Marks the namespace as a production app's (db-provisioner then uses the internal database).
namespaceLabels: {{glwork.net/env: prod}}
# Only the chosen target runs it; every other cluster skips the bundle.
targetCustomizations:
  - name: {s['target']}
    clusterSelector:
      matchLabels: {{glwork.net/target: {s['target']}}}
  - name: other-clusters
    clusterSelector: {{}}
    doNotDeploy: true
""")
    with open(os.path.join(d, "manifests.yaml"), "w") as f:
        f.write(render(s, image))
    print(d)
    return s


def cmd_image_exists(ref):
    """HEAD the manifest with the registry token flow; credentials from ACR_USERNAME / ACR_PASSWORD."""
    m = re.match(r"^([^/]+)/(.+):([^:/]+)$", ref)
    if not m:
        fail(f"image-exists: expected REGISTRY/REPO:TAG, got {ref}")
    host, repo, tag = m.groups()
    basic = base64.b64encode(f"{os.environ['ACR_USERNAME']}:{os.environ['ACR_PASSWORD']}".encode()).decode()
    accept = ", ".join([
        "application/vnd.oci.image.index.v1+json", "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ])
    url = f"https://{host}/v2/{repo}/manifests/{tag}"

    def head(auth):
        req = urllib.request.Request(url, method="HEAD", headers={"Accept": accept, "Authorization": auth})
        return urllib.request.urlopen(req, timeout=20).status

    try:
        head(f"Basic {basic}")
        sys.exit(0)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            sys.exit(1)
        if e.code != 401:
            raise
        challenge = e.headers.get("WWW-Authenticate", "")
    params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
    q = urllib.parse.urlencode({"service": params.get("service", ""), "scope": f"repository:{repo}:pull"})
    req = urllib.request.Request(f"{params['realm']}?{q}", headers={"Authorization": f"Basic {basic}"})
    token = json.load(urllib.request.urlopen(req, timeout=20))
    token = token.get("token") or token.get("access_token")
    try:
        head(f"Bearer {token}")
        sys.exit(0)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            sys.exit(1)
        raise


def main():
    a = sys.argv[1:]
    cmds = {
        ("plan", 0): cmd_plan,
        ("settings", 2): cmd_settings,
        ("render-k8s", 4): cmd_render_k8s,
        ("approval-message", 2): cmd_approval_message,
        ("approval-message", 3): cmd_approval_message,
        ("needs-approval", 2): cmd_needs_approval,
        ("promote-k8s", 4): cmd_promote_k8s,
        ("image-exists", 1): cmd_image_exists,
        ("telegram", 1): cmd_telegram,
        ("wrangler-static", 3): cmd_wrangler_static,
        ("ingress-conflict", 3): cmd_ingress_conflict,
    }
    fn = cmds.get((a[0], len(a) - 1)) if a else None
    if not fn:
        fail(__doc__)
    fn(*a[1:])


if __name__ == "__main__":
    main()
