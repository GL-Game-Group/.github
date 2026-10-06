# 部署应用（给 AI agent）

GL-Game-Group 应用仓库的部署方法。人读的完整版：gl-wiki「工作手册 → 部署运维 → 应用部署」。参考仓库：`GL-Game-Group/vitepress-demo`。

## 流程

```bash
git tag web                        # 选择要发布的应用；版本自动加 1，也可写 web@1.4.0
git push origin main:test web      # 测试：构建并部署到 web.glwork.dev
git push origin main:release       # 正式：同一提交，群管理员在 Telegram 确认后上线 web.glwork.net
```

- 只看推送后分支**最新提交**上的 tag：`<应用>@<版本>` 或 `<应用>`；多个应用用逗号：`web,api`、`web@1.2,api@2.0`。
- 没有 tag 就不部署。`release` 用 `main:release` 直接指向测试过的提交，**不要在 release 上合并**（新提交没有 tag）。
- 简写 tag 发布后会被流水线删除并换成 `<应用>@<版本>`；再次使用前 `git fetch --prune --prune-tags` 或 `git tag -f web`。
- 版本 tag 不要删除或移动。不需要配置任何 GitHub Secret。
- 正式发布只能走审核，agent 不能、也不要尝试绕过。

## 接入（一次性）

1. 仓库需由平台方登记团队（`team`），未登记会报 `may not deploy into`，此时停下来告诉用户。
2. 根目录添加 `deploy.yml`。
3. 从 `GL-Game-Group/vitepress-demo` 原样复制 `.github/workflows/glwork.yml`，不要修改。

## deploy.yml

```yaml
defaults:
  team: game-a              # 测试环境命名空间，必填，须与平台登记一致

apps:
  web:                      # 应用名 = tag 名 = 镜像名，全公司唯一
    context: apps/web       # 构建目录，需有 Dockerfile
    port: 8080
    health: /healthz        # 返回 2xx/3xx 视为就绪
    test: {host: web.glwork.dev}
    prod: {host: web.glwork.net, replicas: 2}

  admin:
    type: worker            # Cloudflare Workers
    workdir: workers/admin  # wrangler.toml 所在目录
    test: {wrangler_env: test}
    prod: {wrangler_env: production, url: https://admin.glwork.net}
```

- 应用下的字段两个环境都生效，`test:` / `prod:` 中的覆盖之；缺 `test:` 不能发测试，缺 `prod:` 不能发正式。
- 只支持 `key: value` 和 `{a: b}`，不支持列表；未知字段直接报错。
- 容器应用字段：`type`(k8s) `team` `target`(office) `namespace`(应用名) `port`(8080) `health`(/) `host` `public`(true) `replicas`(测试 1/正式 2) `cpu`(50m) `memory`(64Mi) `memory_limit`(256Mi) `env_secret` `context`(.) `dockerfile`。
- Worker 字段：`wrangler_env` `workdir` `url`。

## 规则

- 域名：测试 `<名称>.glwork.dev`，正式 `<名称>.glwork.net`，只用一级子域。不能与其他应用重复，不能用 `rancher.glwork.net`、`secrets.glwork.net`；冲突的应用会被拒绝部署。
- 密钥不进 Git：放进命名空间的 Secret，在 `deploy.yml` 写 `env_secret: <Secret 名>`。正式环境的 Secret 由平台方创建。
- 对外服务默认完全开放，服务要自带鉴权。
- 大文件（模型、依赖）在 Dockerfile 中拆成多层。

## 回滚 / 重新部署

Actions → glwork → Run workflow：`env` 填 `test` 或 `prod`，`tag` 填旧版本如 `web@1.3.0`。`prod` 同样需要审核。

## 查看结果

- Actions 运行摘要里有访问地址；Telegram 群「GL服务器运维机器人群」会收到部署成功或失败的通知，失败时附原因。
- 常见报错：

| 报错 | 处理 |
| --- | --- |
| 群里提示「没有应用 tag」 | tag 没推送，或不在分支最新提交上 |
| `no app 'x' in deploy.yml` / `has no 'prod' section` | 应用名写错 / 缺少该环境配置 |
| `unknown field` / `lists are not supported` | 修正 `deploy.yml` 写法 |
| `app name ... is already used by` | 换应用名 |
| `域名 ... 已被 ... 使用` / `平台保留域名` | 换 `host` |
| `rollout status` 超时 | 检查 `port`、`health`、程序是否崩溃 |
