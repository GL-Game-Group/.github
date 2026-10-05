# GL-Game-Group/.github

组织级共享配置。

- `.github/workflows/build-image.yml`：构建镜像并推送到公司镜像仓库（阿里云 ACR 香港，`glwork-registry.cn-hongkong.cr.aliyuncs.com/glwork/<镜像名>`）。
  调用方需要组织或仓库 Secrets：`ACR_USERNAME`、`ACR_PASSWORD`。用法见文件头部注释。
- `.github/workflows/deploy.yml`：按应用仓库的 `deploy.test.yml` / `deploy.prod.yml` 部署。推送 main 自动构建并部署测试环境；
  有 `deploy.prod.yml` 时在 Telegram 群发起正式发布审核，群管理员确认后由 release-bot 触发 `action=promote` 正式发布。
  支持 `type: k8s`（默认）和 `type: worker`（Cloudflare Workers）。
- `scripts/glwork_deploy.py`：解析描述文件、校验平台规则、生成部署清单与审核消息。
