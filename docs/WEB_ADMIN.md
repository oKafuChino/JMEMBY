# 网页管理面板

面板提供用户与任务统计、注册/邀请码开关、用户组展示、封禁/解禁、双队列分页查看、取消任务及漫画下载来源查询。资格授权和白名单设置仍使用群聊回复命令 `/kk`、`/white`。

## 启用

在 `.env` 设置：

```dotenv
WEB_ENABLED=true
WEB_PORT=8080
WEB_PUBLIC_URL=https://admin.example.com
```

同步代码后执行 `docker compose up -d --build --no-deps app`。Compose 将端口绑定至 VPS 的 `127.0.0.1:8080`，需在 VPS 上用 HTTPS 反向代理转发至该地址。域名应专用于面板，不能带路径、查询参数或尾部路径。例如 Nginx 已配置证书的 server 内：

```nginx
location / {
    proxy_pass http://127.0.0.1:8080;
    proxy_set_header Host $host;
}
```

不使用公网域名时，可设置 `WEB_PUBLIC_URL=http://localhost:8080`，电脑执行 `ssh -N -L 8080:127.0.0.1:8080 user@vps`，浏览器打开 `http://localhost:8080`。浏览器地址必须与 WEB_PUBLIC_URL 完全匹配。非 Docker 启动默认只监听本机，可使用 WEB_HOST/WEB_PORT 配置。面板默认关闭，不需要数据库迁移。

## 登录与使用

管理员私聊机器人发送 `/web` 获取登录码，然后在网页粘贴登录。登录码 5 分钟有效、仅用一次，重新获取后旧码失效；会话最长 8 小时，退出、服务重启或新的同账号登录会使原会话失效。只有 TELEGRAM_ALLOWED_USER_IDS 配置的管理员可获取登录码，各接口也重新校验管理员权限。普通用户、白名单、Telegram 群管理员不能登录。

页面支持手动刷新、用户和任务分页、普通/优先队列筛选。封禁、解禁、取消任务、修改开关需要页面二次确认。取消任务保留已发布文件，封禁不撤销 Komga 账号权限。面板不返回 Bot token、Komga 密码、登录凭据或邀请码。

会话使用 HttpOnly、SameSite=Strict Cookie，HTTPS 模式启用 Secure；写操作校验 Origin 和 CSRF token，登录入口有全局频率限制。登录码只通过请求体提交，不放入 URL，HTTP 访问日志默认关闭。请勿将登录码分享给其他人。

验证边界：本地未安装 aiohttp/aiogram 时，HTTP 与 Telegram 路由集成测试会跳过；部署后验证管理员登录、普通用户拒绝、退出失效及任务操作。
