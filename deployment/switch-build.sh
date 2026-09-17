#!/bin/sh
# Compatible code switch only; preserve the original volume and all newer input.
set -eu
if [ "$#" -ne 2 ]; then
  printf '%s\n' '用法：deployment/switch-build.sh 目标镜像ID 原备份操作键' >&2
  exit 2
fi
cd "$(dirname "$0")/.."
target_image=$1
backup_key=$2
# Inspect exact images before stopping; the selected target is never pulled or built here.
target_id=$(docker image inspect --format '{{.Id}}' "$target_image")
current_container=$(docker compose ps --all --quiet iris)
if [ -z "$current_container" ]; then
  printf '%s\n' '没有可确认的原服务容器；请先完成本地安装。' >&2
  exit 2
fi
current_id=$(docker inspect --format '{{.Image}}' "$current_container")
printf '%s\n' "保留原镜像：$current_id" "目标镜像：$target_id"
docker compose stop iris
IRIS_IMAGE=$current_id docker compose run --rm --no-deps iris python -m companion_memory.runtime.managed_cli create-backup "$backup_key"
IRIS_IMAGE=$target_id docker compose run --rm --no-deps iris python -m companion_memory.runtime.managed_cli preflight
if ! IRIS_IMAGE=$target_id docker compose up --detach --no-build --pull never --wait --wait-timeout 60 iris; then
  docker compose stop iris
  printf '%s\n' '目标未通过启动健康检查，已停止目标服务；保留原卷与实际容器供原确认。' >&2
  exit 1
fi
printf '%s\n' '代码切换完成；模型请求许可保持暂停。检查健康与恢复状态后再显式启用。' \
  "原镜像仍为 ${current_id}。需要回退时，以该镜像ID再次执行本命令；将先核验它能读取当前数据。" \
  '预检或启动失败时保留停止状态与实际容器，不自动恢复备份、不删除新数据。'
