# GL-Game-Group/.github

组织级共享配置。

- `.github/workflows/build-image.yml`：构建镜像并推送到公司镜像仓库（阿里云 ACR 香港，`glwork-registry.cn-hongkong.cr.aliyuncs.com/glwork/<镜像名>`），在内网构建机 `gl-internal` 上执行，公开和私有仓库都可用，不需要任何 GitHub Secrets。
- `.github/workflows/deploy.yml`：按应用仓库根目录的 `deploy.yml`（一个仓库可有多个应用，测试和正式配置都在里面）部署。
  应用仓库用 `.github/workflows/glwork.yml` 调用（写法见 deploy.yml 文件头）。发布哪个应用由**推送的提交上的 tag** 决定：
  `<应用>@<版本>`，或只写 `<应用>`（自动按上一个版本加 1 打 tag）。推送 `test` 分支部署测试环境，推送 `release` 分支每个应用在
  Telegram 群发起一条审核，群管理员确认后由 release-bot 发布（容器应用提交到 infra `fleet/apps-prod/<应用>/`，Fleet 部署到
  `target` 指定的集群，默认 `office`；Worker 触发 `action=promote`）。提交上没有应用 tag 时不部署，群里会收到提示。
  可用的 `target` 登记在 `scripts/glwork_deploy.py` 的 `TARGETS`。
- `scripts/glwork_deploy.py`：解析 `deploy.yml`、按 tag 选择应用和版本、校验平台规则、生成部署清单与审核消息（只用标准库）。

所有流水线都运行在内网构建机 `gl-internal` 上（公开和私有仓库都可用），密钥由 External Secrets Operator 从 mgmt 集群
`glwork-secrets` 命名空间同步（在 Rancher 中编辑），GitHub 上不存放任何密钥。
