# GL-Game-Group/.github

组织级共享配置。

- `.github/workflows/build-image.yml`：构建镜像并推送到公司镜像仓库。私有仓库在内网构建机（`gl-internal`）上构建，不需要 GitHub Secrets；公开仓库在 GitHub 托管机器上构建，调用时需写 `secrets: inherit`（使用组织级 `ACR_USERNAME` / `ACR_PASSWORD`）
  （阿里云 ACR 香港，`glwork-registry.cn-hongkong.cr.aliyuncs.com/glwork/<镜像名>`）。不需要任何 GitHub Secrets。
- `.github/workflows/deploy.yml`：在内网构建机上运行，按应用仓库的 `deploy.test.yml` / `deploy.prod.yml` 部署。推送 `test` 分支自动构建并部署测试环境；
  推送 `release` 分支（复用该提交已构建的镜像）在 Telegram 群发起正式发布审核，群管理员确认后由 release-bot 发布：
  容器应用提交到 infra `fleet/apps-prod/`，由 Fleet 部署到 `target` 指定的集群（默认 `office`，公司内网集群）；Worker 触发 `action=promote`。
  可用的 `target` 登记在 `scripts/glwork_deploy.py` 的 `TARGETS`。
  支持 `type: k8s`（默认）和 `type: worker`（Cloudflare Workers）。
- `scripts/glwork_deploy.py`：解析描述文件、校验平台规则、生成部署清单与审核消息。

所有流水线都运行在内网构建机 `gl-internal` 上（只服务私有仓库），密钥由 External Secrets Operator 从 mgmt 集群
`glwork-secrets` 命名空间同步（在 Rancher 中编辑），GitHub 上不存放任何密钥。
