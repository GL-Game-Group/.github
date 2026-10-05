# GL-Game-Group/.github

组织级共享配置。

- `.github/workflows/build-image.yml`：构建镜像并推送到公司镜像仓库（阿里云 ACR 香港，`glwork-registry.cn-hongkong.cr.aliyuncs.com/glwork/<镜像名>`）。
  调用方需要组织或仓库 Secrets：`ACR_USERNAME`、`ACR_PASSWORD`。用法见文件头部注释。
