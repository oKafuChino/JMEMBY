"""Human-readable schedule results, without source responses or credentials."""
REASONS = {
    "AI_TAG": "AI 生成标签", "AI_TITLE": "标题标注 AI 生成", "AI_FLAG": "来源标记 AI 生成",
    "METADATA_INCOMPLETE": "元数据不完整", "TARGET_TAG_UNCONFIRMED": "未确认目标标签",
    "LIKES_UNKNOWN": "精确点赞数未知", "AUTO_ALREADY_ATTEMPTED": "此前已自动尝试",
    "ACTIVE_TASK": "已有下载任务", "ALREADY_IMPORTED": "已经入库", "NEEDS_REPAIR": "已有文件需人工检查",
    "NOT_SELECTED": "超过本次数量", "MATCH": "标签和内容检查通过", "RETRY_PENDING": "等待重新检查",
    "DISCOVERY_UNSUPPORTED": "来源不支持点赞搜索，计划已暂停", "BACKPRESSURE": "自动队列达到上限",
    "DISCOVERY_BUDGET": "运行预算已用尽", "DISCOVERY_TIMEOUT": "搜索超时",
    "SCHEDULE_CANCELLED": "计划已暂停或权限变化", "RATE_LIMIT": "来源限流",
    "NETWORK": "来源连接失败", "PROVIDER_PROTOCOL": "来源返回格式无效",
}
SORT_LABELS = {"SOURCE_LIKES_DESC": "上游点赞降序", "EXACT_LIKES_DESC": "候选范围内精确点赞降序"}
RUN_LABELS = {"DISCOVERING": "选书中", "PLANNED": "待入队", "QUEUED": "已入队，等待下载结果",
              "FINISHED": "全部完成", "PARTIAL": "部分完成", "FAILED": "失败", "CANCELLED": "已取消",
              "CANCELLING": "正在取消", "EMPTY": "无可入队作品", "MISSED": "错过执行时间",
              "OVERLAP_SKIPPED": "上次未结束，跳过本次"}


def reason_label(value):
    return REASONS.get(value, value)
