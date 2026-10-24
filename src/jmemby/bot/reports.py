"""Send persisted reports to configured admins; failed deliveries retry separately."""
import logging

logger = logging.getLogger(__name__)


async def deliver_reports(bot, community):
    for notice in community.pending_reports():
        report_id, admin = notice['report_id'], notice['admin_id']
        if admin not in community.users.admin_ids or not community.report_pending(report_id):
            community.delivered(report_id, admin, 'SKIPPED')
            continue
        text = (f"🚩 漫画内容举报 #{report_id}\n漫画：{notice['title'][:250]}\n"
                f"漫画 ID：{notice['source_id']}\n举报用户 ID：{notice['user_id']}\n"
                "请通过 /web 登录管理面板，在「举报管理」查看并处理。此消息仅作通知。")
        try:
            await bot.send_message(admin, text, parse_mode=None)
            community.delivered(report_id, admin)
        except Exception as exc:
            retry_after = getattr(exc, 'retry_after', None)
            delay = retry_after if type(retry_after) in (int, float) and retry_after > 0 else min(3600, 2 ** min(notice['attempts']+1, 11))
            community.defer(report_id, admin, delay)
            logger.warning('report_delivery_failed report=%s admin=%s type=%s', report_id, admin, type(exc).__name__)
