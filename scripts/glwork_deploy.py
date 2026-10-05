#!/usr/bin/env python3
"""glwork deploy helper used by .github/workflows/deploy.yml.

Reads deploy.test.yml / deploy.prod.yml from an application repository, validates them
against the platform rules and renders what the workflow needs.

  glwork_deploy.py plan                         -> GITHUB_OUTPUT: test/prod settings as JSON
  glwork_deploy.py render-k8s ENV IMAGE         -> Kubernetes manifests on stdout
  glwork_deploy.py approval-message             -> Telegram sendMessage JSON on stdout
  glwork_deploy.py promote-k8s INFRA_DIR IMAGE  -> write the production bundle into infra

Only the standard library is used. The descriptor files are a small YAML subset:
"key: value" lines, '#' comments, no nesting.
"""
import json
import os
import re
import sys

TARGETS = {"aliyun-prod": "aliyun", "aws-prod": "aws"}
FRPS_IP = "8.217.141.116"
REGISTRY = "glwork-registry.cn-hongkong.cr.aliyuncs.com/glwork"
NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,38}[a-z0-9])?$")
TEST_HOST_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?\.(int\.)?glwork\.dev$")
PROD_HOST_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?\.glwork\.net$")


def fail(msg):
    print(f"::error::{msg}", file=sys.stderr)
    sys.exit(2)


def load(path):
    if not os.path.exists(path):
        return None
    out = {}
    for n, raw in enumerate(open(path, encoding="utf-8"), 1):
        line = re.sub(r"\s+#.*$", "", raw.rstrip("\n"))
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$", line)
        if not m or line.startswith((" ", "\t")):
            fail(f"{path}:{n}: expected 'key: value' (no nesting): {raw.strip()}")
        v = m.group(2).strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        out[m.group(1)] = v
    return out


def repo_name():
    return os.environ.get("GITHUB_REPOSITORY", "local/app").split("/", 1)[1].lower()


def settings(env):
    path = f"deploy.{env}.yml"
    d = load(path)
    if d is None:
        return None
    s = {"env": env, "file": path}
    s["type"] = d.get("type", "k8s")
    if s["type"] not in ("k8s", "worker"):
        fail(f"{path}: type must be k8s or worker")
    s["name"] = d.get("name", repo_name())
    if not NAME_RE.match(s["name"]):
        fail(f"{path}: name '{s['name']}' must be lowercase letters, digits and '-' (max 40)")
    if s["type"] == "worker":
        s["wrangler_env"] = d.get("wrangler_env", "test" if env == "test" else "production")
        s["url"] = d.get("url", "")
        s["workdir"] = d.get("workdir", ".")
        return s
    # k8s
    s["port"] = d.get("port", "8080")
    if not s["port"].isdigit():
        fail(f"{path}: port must be a number")
    s["health"] = d.get("health", "/")
    s["replicas"] = d.get("replicas", "1" if env == "test" else "2")
    s["cpu"] = d.get("cpu", "50m")
    s["memory"] = d.get("memory", "64Mi")
    s["memory_limit"] = d.get("memory_limit", "256Mi")
    s["env_secret"] = d.get("env_secret", "")
    s["context"] = d.get("context", ".")
    s["dockerfile"] = d.get("dockerfile", "")
    s["public"] = d.get("public", "true") == "true"
    if env == "test":
        s["team"] = d.get("team", "")
        if not NAME_RE.match(s["team"] or "-"):
            fail(f"{path}: team is required (the namespace assigned by the platform)")
        s["namespace"] = s["team"]
        s["secret"] = "KUBECONFIG_" + s["team"].upper().replace("-", "_")
        s["host"] = d.get("host", f"{s['name']}.glwork.dev")
        if s["public"] and not TEST_HOST_RE.match(s["host"]):
            fail(f"{path}: host '{s['host']}' must be <name>.glwork.dev or <name>.int.glwork.dev")
    else:
        s["target"] = d.get("target", "")
        if s["target"] not in TARGETS:
            fail(f"{path}: target must be one of {', '.join(TARGETS)}")
        s["namespace"] = d.get("namespace", s["name"])
        s["host"] = d.get("host", f"{s['name']}.glwork.net")
        if s["public"] and not PROD_HOST_RE.match(s["host"]):
            fail(f"{path}: host '{s['host']}' must be <name>.glwork.net")
        s["ingress_target"] = d.get("ingress_target", "")  # cluster entry IP, set by the platform
    return s


def out(key, value):
    with open(os.environ.get("GITHUB_OUTPUT", "/dev/stdout"), "a") as f:
        f.write(f"{key}={value}\n")


def cmd_plan():
    test, prod = settings("test"), settings("prod")
    if not test and not prod:
        fail("no deploy.test.yml or deploy.prod.yml in the repository root")
    if test and prod and test["type"] != prod["type"]:
        fail("deploy.test.yml and deploy.prod.yml must use the same type")
    kind = (test or prod)["type"]
    name = (test or prod)["name"]
    out("type", kind)
    out("name", name)
    out("image", f"{REGISTRY}/{name}")
    out("test", json.dumps(test or {}))
    out("prod", json.dumps(prod or {}))
    out("has_test", "true" if test else "false")
    out("has_prod", "true" if prod else "false")
    print(json.dumps({"type": kind, "name": name, "test": test, "prod": prod}, ensure_ascii=False, indent=2))


def render(s, image):
    sha = os.environ.get("GITHUB_SHA", "")
    by = os.environ.get("DEPLOYED_BY", os.environ.get("GITHUB_ACTOR", ""))
    via = os.environ.get("DEPLOY_VIA", "ci")
    n = s["name"]
    labels = f"{{app.kubernetes.io/name: {n}, app.kubernetes.io/managed-by: glwork-deploy}}"
    env_from = f"          envFrom: [{{secretRef: {{name: {s['env_secret']}}}}}]\n" if s["env_secret"] else ""
    y = f"""apiVersion: apps/v1
kind: Deployment
metadata:
  name: {n}
  namespace: {s['namespace']}
  labels: {labels}
  annotations:
    glwork.net/deployed-by: {json.dumps(by)}
    glwork.net/commit: {json.dumps(sha[:12])}
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
        if s["env"] == "test":
            ann = f'    external-dns.kubernetes.io/target: "{FRPS_IP}"\n    external-dns.kubernetes.io/cloudflare-proxied: "false"\n'
        else:
            target = s["ingress_target"] or "REPLACE_WITH_CLUSTER_ENTRY_IP"
            ann = f'    external-dns.kubernetes.io/target: "{target}"\n    external-dns.kubernetes.io/cloudflare-proxied: "true"\n'
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


def cmd_render_k8s(env, image):
    s = settings(env)
    if not s or s["type"] != "k8s":
        fail(f"deploy.{env}.yml is not a k8s descriptor")
    sys.stdout.write(render(s, image))


def cmd_approval_message():
    """Message the release-bot acts on. The last line carries the request in machine-readable form."""
    s = settings("prod")
    repo = os.environ["GITHUB_REPOSITORY"]
    sha = os.environ["GITHUB_SHA"]
    run = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{repo}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"
    title = os.environ.get("COMMIT_TITLE", "").strip()[:80]
    version = os.environ.get("WORKER_VERSION", "")
    target = s.get("target") or f"worker:{s['wrangler_env']}"
    esc = lambda t: t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    lines = [
        f"🟡 <b>待审核</b> · 正式发布 → <b>{esc(target)}</b>",
        f"应用：<code>{esc(repo)}</code>（{s['type']}）",
        f"版本：<code>{sha[:12]}</code> {esc(title)}",
        f"发起人：{esc(os.environ.get('GITHUB_ACTOR', ''))}",
        f"构建：{run}",
        f"提交：https://github.com/{repo}/commit/{sha}",
    ]
    if version:
        lines.append(f"Worker 版本：<code>{esc(version)}</code>")
    lines.append("群管理员点击下方按钮审核，24 小时内有效。")
    req = {"r": repo, "s": sha, "t": target, "k": s["type"], "v": version}
    lines.append(f"<code>req {esc(json.dumps(req, separators=(',', ':')))}</code>")
    body = {
        "chat_id": os.environ["TELEGRAM_CHAT_ID"],
        "text": "\n".join(lines),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": {"inline_keyboard": [[
            {"text": "✅ 确认", "callback_data": "approve"},
            {"text": "❌ 拒绝", "callback_data": "reject"},
        ]]},
    }
    print(json.dumps(body, ensure_ascii=False))


def cmd_promote_k8s(infra_dir, image):
    """Write fleet/apps-prod/<name>/ into the infra checkout (Fleet deploys it to the target cluster)."""
    s = settings("prod")
    d = os.path.join(infra_dir, "fleet", "apps-prod", s["name"])
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "fleet.yaml"), "w") as f:
        f.write(f"""# Managed by GL-Game-Group/.github deploy.yml (production release of {os.environ.get('GITHUB_REPOSITORY', '')}).
defaultNamespace: {s['namespace']}
# Only the chosen target runs it; every other production cluster skips the bundle.
targetCustomizations:
  - name: {s['target']}
    clusterSelector:
      matchLabels: {{env: production, site: {TARGETS[s['target']]}}}
  - name: other-clusters
    clusterSelector: {{}}
    doNotDeploy: true
""")
    with open(os.path.join(d, "manifests.yaml"), "w") as f:
        f.write(render(s, image))
    print(d)


def main():
    a = sys.argv[1:]
    if a[:1] == ["plan"]:
        cmd_plan()
    elif a[:1] == ["render-k8s"] and len(a) == 3:
        cmd_render_k8s(a[1], a[2])
    elif a[:1] == ["approval-message"]:
        cmd_approval_message()
    elif a[:1] == ["promote-k8s"] and len(a) == 3:
        cmd_promote_k8s(a[1], a[2])
    else:
        fail(__doc__)


if __name__ == "__main__":
    main()
