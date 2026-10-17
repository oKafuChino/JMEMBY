# 适配器进程协议

目前仓库内置 JMComic 和 Komga 实现，命令配置留空即可使用。下方“待实现”的文字为初始设计记录，当前真实服务兼容验证仍待完成。自定义适配器可继续遵循本协议。

当前为了不猜测第三方 API，将不稳定的供应方调用隔离在子进程。生产适配器尚未实现；不要配置任意下载脚本后直接上线。

`PROVIDER_COMMAND` / `KOMGA_COMMAND` 为 JSON 字符串数组，例如 `["python", "-m", "my_verified_adapter"]`。必须在容器镜像中安装对应代码及依赖。应用不使用 shell 执行命令。

一次调用：stdin 输入一行 JSON，stdout 输出一个 JSON 对象，退出码为 0。stdout 不得输出调试日志。进程执行有超时和 8 MiB 输出限制。Linux 取消时终止整个进程组；Windows 只保证直接子进程，Windows 适配器不得再启动后台子进程。

## 数据源

请求 `{"operation":"metadata","id":"42"}`。

响应 `{"title":"测试漫画","authors":["作者"],"chapters":[{"id":"52","title":"第一章"}]}`。章节按阅读顺序返回，ID 必须稳定。

请求 `{"operation":"pages","chapter_id":"52"}`。

响应 `{"pages":["由适配器解释的图片标识1","图片标识2"]}`。数组顺序为页序，标识应可跨任务恢复；若供应方只有过期 URL，适配器应使用稳定页 ID 并在下载时刷新 URL。

请求 `{"operation":"download","url":"图片标识","destination":"绝对临时文件路径","max_bytes":31457280}`。

响应 `{}`。适配器需以流式方式执行字节限制，处理图片还原，写入指定路径后再退出。还原后的文件也必须在限制内，不得另存任意位置。应用会再次解码和校验，但不能替代下载时的流量/磁盘限制。

## 可选的定时标签发现能力

以下两项为定时计划使用的扩展；旧适配器缺少能力时应返回 `DISCOVERY_UNSUPPORTED`，不能返回默认热度结果。原有按 ID 下载协议保持兼容。

搜索请求：`{"operation":"search_tag","tag":"冒险","page":1}`。

响应：`{"ids":["42","43"],"last":true,"mode":"SOURCE_LIKES_DESC","scope":"all"}`。ID 必须为数字字符串，单页最多 200 个；last 为布尔值；scope 明确查询时间范围。SOURCE_LIKES_DESC 要求已确认启用上游点赞降序，页序和页内顺序即 rank。若只有精确计数可用，返回 EXACT_LIKES_DESC，调用方会收集预算内候选并排序；禁止以浏览量或缩写计数冒充点赞排序。

严格详情请求：`{"operation":"discovery_detail","id":"42"}`。

响应示例：

```json
{
  "manga": {
    "source": "jmcomic", "source_id": "42", "title": "测试漫画", "authors": ["作者"],
    "chapters": [{"source_id": "52", "ordinal": 1, "title": "第一章", "pages": []}]
  },
  "tags": ["冒险"], "markers": [], "complete": true, "ai_flag": false,
  "likes_raw": "1K", "likes_exact": null
}
```

tags 只含完整的普通标签，markers 为可用的作品、人物、分类标记。缺失、错误、被截断的必检字段必须使 complete=false，不能填空后宣称完整。每个标签/标记字段最多 1000 项、每项最多 1000 字符，超出视为不完整。ai_flag 为已知 AI 生成布尔标志；likes_exact 只能是非负精确整数或 null，1K/1W 等缩写保留在 likes_raw。

所有响应仍受总输出限制。严格详情在选书及自动任务下载前都会调用；暂时失败应用应返回可重试错误，而非伪造空标签。

## Komga 入库操作

请求 `{"operation":"import","paths":["漫画目录/章节.cbz"]}`。

适配器从环境变量读取 Komga 地址、鉴权、library ID、容器根路径。根据已验证版本触发扫描，并分页查询实际入库文件；严格匹配 library 和完整路径，全部完成后返回：

`{"books":{"漫画目录/章节.cbz":"已确认的Komga书籍ID"}}`。

只返回扫描请求接受结果不满足协议。若暂未入库，返回可重试错误。不得用标题或返回数量推断全部入库。应用显示公共访问入口，不生成未经验证的深链接。

## 错误

`{"error":{"code":"RATE_LIMIT","retry_after":30}}`。

可重试 code：TIMEOUT、RATE_LIMIT、NETWORK、SERVER_ERROR。其他 code 为终止错误。详细响应/凭据不能出现在 stdout 错误中。建议适配器不做额外自动重试，由主任务统一安排。

## 接下来必须完成的真实集成

1. 核实 jmcomic 客户端版本、许可证、调用方式、图片还原及取消行为，编写协议适配模块与依赖锁定。
2. 核实固定 Komga 版本的鉴权、扫描和分页查询 API，编写协议适配模块。
3. 在有凭据的环境使用授权小样本验证完整链路并补充显式启用的集成测试。
4. 在镜像中安装适配器，将经过验证的命令写入配置示例。
