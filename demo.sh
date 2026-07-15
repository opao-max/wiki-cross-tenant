#!/bin/bash
set -e

# ============================================================
# 答辩演示脚本 — 跨租户知识库迁移
#
# 使用前需确认/调整以下参数：
#   SOURCE_PROFILE  — 源端 lark-cli profile 名
#   TARGET_PROFILE  — 目标端 lark-cli profile 名
#   SOURCE_SPACE    — 源端知识空间 ID
#   TARGET_SPACE    — 目标端知识空间 ID
#   SOURCE_ROOT     — 源端要迁移的子树根节点（空=整个空间）
#   TARGET_ROOT     — 目标端挂载的父节点（空=顶级）
#   SOURCE_DOMAIN   — 源端飞书域名
#   TARGET_DOMAIN   — 目标端飞书域名
#   TARGET_MEMBER   — 目标用户在源租户的 open_id
# ============================================================

SOURCE_PROFILE="seewo"
TARGET_PROFILE="xupt"
SOURCE_SPACE="7085240270054064156"
TARGET_SPACE="7633729764468985012"
SOURCE_ROOT="Vg7ew3xb8izCA8k5NBfcNvsJnXg"
TARGET_ROOT="U8aowt0RXiC0vHkxawccQZ0Enrb"
SOURCE_DOMAIN="agqg3o3wxu.feishu.cn"
TARGET_DOMAIN="rcnwnx20zrwi.feishu.cn"
TARGET_MEMBER="ou_bb17260b4370d1463d1b71ceb71f7de2"

cd "$(dirname "$0")"
rm -rf state/

python3 preflight.py \
  --source-profile "$SOURCE_PROFILE" --target-profile "$TARGET_PROFILE" \
  --source-space "$SOURCE_SPACE" --target-space "$TARGET_SPACE" \
  --source-root "$SOURCE_ROOT" --target-root "$TARGET_ROOT" \
  --target-member-id "$TARGET_MEMBER"

python3 sync.py \
  --source-profile "$SOURCE_PROFILE" --target-profile "$TARGET_PROFILE" \
  --source-space "$SOURCE_SPACE" --target-space "$TARGET_SPACE" \
  --source-root "$SOURCE_ROOT" --target-root "$TARGET_ROOT" \
  --source-domain "$SOURCE_DOMAIN" --target-domain "$TARGET_DOMAIN" \
  --target-member-id "$TARGET_MEMBER"
