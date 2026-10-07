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
- 容器应用字段：`type`(k8s) `team` `target`(office) `namespace`(应用名) `port`(8080) `health`(/) `host` `public`(true) `replicas`(测试 1/正式 2) `cpu`(50m) `memory`(64Mi) `memory_limit`(256Mi) `env_secret` `database` `context`(.) `dockerfile`。
- Worker 字段：`wrangler_env` `workdir` `url`。
- 纯静态网站用 `type: static`，不要写 wrangler 配置：字段 `workdir`(.) `build` `output`(dist) `spa`(false) `host`。正式环境审核通过后才构建上线。

  ```yaml
  landing:
    type: static
    build: npm run build
    output: dist
    test: {host: landing.glwork.dev}
    prod: {host: landing.glwork.net}
  ```

## 规则

- 域名：测试 `<名称>.glwork.dev`，正式 `<名称>.glwork.net`，只用一级子域。不能与其他应用重复，不能用 `rancher.glwork.net`、`secrets.glwork.net`；冲突的应用会被拒绝部署。
- 密钥不进 Git：放进命名空间的 Secret，在 `deploy.yml` 写 `env_secret: <Secret 名>`。正式环境的 Secret 由平台方创建。
- 需要 PostgreSQL：写 `database: postgres`（仅 k8s 应用），测试 / 正式环境各自自动建库，应用读环境变量 `DATABASE_URL`（或 `PGHOST` `PGPORT` `PGDATABASE` `PGUSER` `PGPASSWORD`）。不要自己建库或把连接串写进代码。
- 对外服务默认完全开放，服务要自带鉴权。
- 大文件（模型、依赖）在 Dockerfile 中拆成多层。

## 发布指定版本 / 回滚

部署只用推送分支：把 `test` / `release` 指向某个版本 tag 所在的提交，就是发布这个版本（不生成新版本号）。

```bash
git push origin 'web@1.4.3^{}:refs/heads/release'      # 把测试过的 web@1.4.3 发布为正式版
git push -f origin 'web@1.4.2^{}:refs/heads/release'   # 回滚（分支往回移需要 -f；仍需审核）
git push -f origin 'web@1.4.2^{}:refs/heads/test'      # 测试环境换回旧版本
```

`-f` 只用于 `test` / `release`，绝不用在 `main` 上。同一提交上其他应用的版本 tag 也会一起部署 / 发起审核。

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
