import asyncio
import secrets

from aiohttp import web

from jmemby.web.ui import PAGE
from jmemby.web.batch import BatchDownloads
from jmemby.services.community import CommunityService


def create_app(service, auth, settings):
    users, store = service.users, service.store
    secure = settings.web_public_url.startswith('https://')

    @web.middleware
    async def guard(request, handler):
        try:
            if request.method != 'GET':
                if request.headers.get('Origin') != settings.web_public_url:
                    raise web.HTTPForbidden(text='请求来源无效')
                if request.content_type != 'application/json':
                    raise web.HTTPUnsupportedMediaType()
            if request.path not in {'/', '/api/login'}:
                try:
                    session = auth.authenticate(request.cookies.get('session', ''))
                except ValueError:
                    raise web.HTTPUnauthorized(text='请先登录')
                request['admin'] = session['user_id']
                if request.method != 'GET' and not secrets.compare_digest(
                        request.headers.get('X-CSRF-Token', ''), session['csrf']):
                    raise web.HTTPForbidden(text='会话校验失败，请刷新页面')
            response = await handler(request)
        except (ValueError, TypeError, KeyError) as exc:
            response = web.json_response({'error': str(exc)[:300]}, status=400)
        except web.HTTPException as exc:
            response = web.json_response({'error': exc.text}, status=exc.status)
        response.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
                                 'Referrer-Policy': 'no-referrer', 'X-Frame-Options': 'DENY',
                                 'Content-Security-Policy': "default-src 'none'; script-src 'nonce-" + request.get('nonce', '') + "'; style-src 'nonce-" + request.get('nonce', '') + "'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        return response

    app = web.Application(middlewares=[guard], client_max_size=65536)
    batches = BatchDownloads(service)
    community = CommunityService(store, users)

    async def batch_lifecycle(app):
        yield
        await batches.close()

    app.cleanup_ctx.append(batch_lifecycle)

    async def batch_start(request):
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError('请求必须是 JSON 对象')
        # Actor/chat/priority always come from authenticated server state.
        return web.json_response(batches.start(request['admin'], data.get('text')), status=202)

    async def batch_status(request):
        return web.json_response(batches.snapshot(request['admin'], request.match_info['batch_id']))

    async def batch_latest(request):
        return web.json_response({'batch': batches.latest(request['admin'])})

    async def index(request):
        request['nonce'] = secrets.token_urlsafe(24)
        return web.Response(text=PAGE.replace('__NONCE__', request['nonce']), content_type='text/html')

    async def login(request):
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError('请求必须是 JSON 对象')
        code = data.get('code', '')
        if not isinstance(code, str):
            raise ValueError('登录码格式无效')
        token = auth.login(code)
        response = web.json_response({'ok': True})
        response.set_cookie('session', token, httponly=True, secure=secure, samesite='Strict', max_age=28800, path='/')
        return response

    async def session(request):
        record = auth.authenticate(request.cookies.get('session', ''))
        return web.json_response({'user_id': record['user_id'], 'csrf': record['csrf']})

    async def logout(request):
        auth.logout(request.cookies.get('session', ''))
        response = web.json_response({'ok': True})
        response.del_cookie('session', path='/')
        return response

    def page_number(request):
        page = int(request.query.get('page', '1'))
        if not 1 <= page <= 1000000:
            raise ValueError('页码无效')
        return page

    async def overview(request):
        stats = users.statistics(request['admin'])
        coordinator = getattr(service, 'download_coordinator', None)
        stats['distributed'] = coordinator.snapshot() if coordinator else {'enabled': False}
        stats['tasks'] = {r['status']: r['n'] for r in store.connection.execute(
            'SELECT status,COUNT(*) n FROM tasks GROUP BY status')}
        return web.json_response(stats)

    async def nodes(request):
        coordinator = getattr(service, 'download_coordinator', None)
        if not coordinator or not coordinator.registry:
            return web.json_response({'enabled': False, 'agents': [], 'public_url': ''})
        return web.json_response({**coordinator.snapshot(), 'public_url': coordinator.registry.origin()})

    async def node_action(request):
        coordinator = getattr(service, 'download_coordinator', None)
        if not coordinator or not coordinator.registry:
            raise ValueError('节点调度服务不可用，请检查主控启动日志')
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError('请求必须是 JSON 对象')
        registry, operation = coordinator.registry, data.get('action')
        name = data.get('id')
        if operation == 'origin':
            return web.json_response({'public_url': registry.set_origin(data.get('public_url'))})
        if operation == 'create':
            if not registry.origin():
                raise ValueError('请先保存 Agent HTTPS 地址')
            registry.create(name, data.get('label'), data.get('concurrency'), data.get('queue_scope', 'all'))
            return web.json_response(registry.issue(name), status=201)
        if not isinstance(name, str):
            raise ValueError('节点 ID 无效')
        if operation == 'install':
            return web.json_response(registry.issue(name))
        if operation == 'test':
            return web.json_response(await coordinator.test_connection(name))
        if operation == 'update':
            registry.update(name, data.get('label'), data.get('concurrency'),
                            data.get('queue_scope', registry.get(name)['queue_scope']))
        elif operation == 'enabled':
            registry.set_enabled(name, data.get('enabled'))
            coordinator.invalidate(name)
        elif operation == 'delete':
            if name in coordinator.books:
                raise ValueError('该节点还有运行任务，请先停用并等待任务回收后删除')
            registry.delete(name)
            coordinator.invalidate(name)
        else:
            raise ValueError('不支持的节点操作')
        return web.json_response({'ok': True})

    async def members(request):
        rows = users.list_users(request['admin'], page_number(request), request.query.get('q', ''))
        for row in rows:
            row['is_admin'] = users.is_admin(row['user_id'])
        return web.json_response(rows)

    async def tasks(request):
        queue = request.query.get('queue', 'all')
        if queue not in {'all', 'normal', 'white'}:
            raise ValueError('队列无效')
        where = '' if queue == 'all' else ' WHERE t.priority=' + ('1' if queue == 'white' else '0')
        rows = store.connection.execute(
            'SELECT t.id,m.title,m.source_id,t.user_id,t.status,t.priority,t.progress,t.created_at '
            'FROM tasks t JOIN manga m ON m.id=t.manga_id' + where +
            " ORDER BY CASE WHEN t.status IN ('COMPLETED','FAILED','CANCELLED') THEN 1 ELSE 0 END,t.priority DESC,t.queue_sequence LIMIT 20 OFFSET ?",
            ((page_number(request) - 1) * 20,)).fetchall()
        coordinator = getattr(service, 'download_coordinator', None)
        assignments = {a['task']['task_id']: {'id': a['id'], 'label': a['label']}
                       for a in coordinator.snapshot()['agents'] if a.get('task')} if coordinator else {}
        return web.json_response([{**dict(r), 'node': assignments.get(r['id'])} for r in rows])

    async def history(request):
        rows = service.recent_downloads(request['admin'], request.query.get('q', ''))
        return web.json_response({'tasks': rows, 'limit': 100})

    async def library(request):
        manager = getattr(service, 'library', None)
        if manager is None:
            raise ValueError('漫画库管理服务不可用')
        if request.method == 'GET':
            return web.json_response(manager.list(request['admin'], page_number(request), request.query.get('q', '')))
        data = await request.json()
        if not isinstance(data, dict) or data.get('confirm') is not True:
            raise ValueError('请确认移除漫画文件')
        return web.json_response(await manager.remove(request['admin'], data.get('manga_id')))

    async def reports(request):
        if request.method == 'GET':
            return web.json_response(community.review_list(request['admin'], page_number(request),
                request.query.get('status', 'PENDING'), request.query.get('q', '')))
        data = await request.json()
        if not isinstance(data, dict) or data.get('confirm') is not True:
            raise ValueError('请确认举报处理操作')
        operation, report_id = data.get('action'), data.get('report_id')
        if operation == 'dismiss':
            community.dismiss_report(request['admin'], report_id)
            return web.json_response({'ok': True})
        if operation == 'remove':
            report = community.review_target(request['admin'], report_id)
            manager = getattr(service, 'library', None)
            if manager is None:
                raise ValueError('漫画库管理服务不可用')
            # File removal closes all pending reports for the manga before awaiting a scan.
            return web.json_response(await manager.remove(request['admin'], report['manga_id']))
        raise ValueError('不支持的举报处理操作')

    async def action(request):
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError('请求必须是 JSON 对象')
        actor = request['admin']
        operation = data.get('action')
        if operation in {'registration', 'invite_mode'}:
            enabled = data.get('enabled')
            if type(enabled) is not bool:
                raise ValueError('开关值无效')
            (users.set_registration if operation == 'registration' else users.set_invite_required)(actor, enabled)
        elif operation in {'ban', 'unban'}:
            users.set_blocked(actor, int(data['user_id']), operation == 'ban')
        elif operation == 'cancel':
            task = store.get(str(data['task_id']))
            if not task:
                raise ValueError('任务不存在')
            store.request_cancel(task['id'], task['user_id'])
        else:
            raise ValueError('不支持的操作')
        return web.json_response({'ok': True})

    app.add_routes([web.get('/', index), web.post('/api/login', login), web.get('/api/session', session),
                    web.post('/api/logout', logout), web.get('/api/overview', overview),
                    web.get('/api/nodes', nodes), web.post('/api/nodes', node_action),
                    web.get('/api/users', members), web.get('/api/tasks', tasks),
                    web.post('/api/batches', batch_start), web.get('/api/batches', batch_latest),
                    web.get('/api/batches/{batch_id}', batch_status),
                    web.get('/api/history', history), web.post('/api/action', action),
                    web.get('/api/library', library), web.post('/api/library/remove', library),
                    web.get('/api/reports', reports), web.post('/api/reports', reports)])
    return app


async def serve(service, auth, settings):
    runner = web.AppRunner(create_app(service, auth, settings), access_log=None)
    try:
        await runner.setup()
        await web.TCPSite(runner, settings.web_host, settings.web_port).start()
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
