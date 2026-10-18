将网关私有 CA 放在此目录，并将 `.env.python` 的 `HD_MODEL_CA_FILE` 设为 `/run/certs/文件名`。目录只读挂载到 API 和 Worker；证书文件不会加入 Git 或镜像。无需私有 CA 时留空该设置。
