# facebook-mcp 架构设计

**语言**:[中文](./ARCHITECTURE.md) · [English](./ARCHITECTURE.en.md)

> 目标:把「Facebook 数据获取 / 发消息 / 关键词搜索」封装成 MCP Server,供 LLM Agent 调用。
> 设计原则:**高内聚、低耦合、易扩展、单向依赖**。
>
> 本文档描述的是**当前已实现的系统**,不是初始设想。技术结论均来自真机实测,
> 证据见 §8。

---

## 1. 目标与范围

| 能力 | MCP 工具 | 示例 | 是否需要登录 | 状态 |
|---|---|---|---|---|
| 获取用户最新帖子时间 | `get_user_latest_post_time` | `https://www.facebook.com/exampleuser` | 否(公开) | ✅ |
| 获取最近帖子列表 | `get_user_recent_posts` | 同上 | 否(公开) | ✅ |
| 关键词搜索:人 | `search_people` | `/search/people?q=pest%20control` | **是** | ✅ |
| 关键词搜索:主页 | `search_pages` | `/search/pages?q=pest%20control` | **是** | ✅ |
| 发送消息 (Messenger) | `send_message` | 给某用户/主页发私信 | **是 + 浏览器** | ✅ |

> **授权说明**:本项目仅用于**自有账号 / 已获授权**平台的自动化与接口对接。
> 发送消息、搜索等行为需遵守 Facebook 平台条款与当地法律(反垃圾消息、隐私法规等)。

---

## 2. 技术选型

| 关注点 | 选型 | 理由 |
|---|---|---|
| MCP 框架 | **官方 `mcp` SDK(`>=1.2,<2`)** → `mcp.server.fastmcp.FastMCP` | 官方维护,装饰器即工具,类型安全,自带 stdio / SSE / streamable-http |
| HTTP 传输 | **curl_cffi** | 模拟 Chrome 的 TLS/JA3 指纹,纯 Python |
| 浏览器通道 | **Chrome DevTools Protocol**(`websocket-client` 直连) | 复用已登录 Chrome 的 cookie 与执行环境;**未使用 Playwright/Selenium** |
| 抓包/接口发现 | **CDP `Network` 域**(`tools/sniff.py`) | 无需额外依赖,直接读取真实请求的 `doc_id` 与 `variables` |
| 数据模型 | **pydantic v2** + **pydantic-settings** | 结构化模型、配置校验 |
| HTML 解析 | **beautifulsoup4 + lxml** | 抽取页面内嵌的 ServerJS JSON |
| 运行时 | **Python 3.11+** | `mcp<2` 要求 ≥3.10,本机默认 `python3` 是 3.8,不可用 |

> ⚠️ 环境:必须用 `/opt/homebrew/bin/python3.11 -m venv .venv` 建独立环境。
>
> 注:`mcp-wireshark` 与 `camoufox-reverse-mcp` **未被使用**。实际抓包由 CDP 的
> `Network`/`Fetch` 域完成;未遇到需要 JSVMP 逆向的签名类风控(`jazoest` 已可纯推导,见 §8)。

---

## 3. 分层架构(单向依赖)

```
┌──────────────────────────────────────────────────────────────────┐
│ L5  server.py          MCP 接口层(5 个工具)                       │
│     get_user_latest_post_time / get_user_recent_posts /           │
│     search_people / search_pages / send_message                   │
└───────────────────────────────┬──────────────────────────────────┘
                                ↓ 只依赖 core + services
┌──────────────────────────────────────────────────────────────────┐
│ L4  services/          服务层(用例编排)                            │
│     post_service / search_service / message_service               │
│     factory.py —— 组合根(唯一装配 transport/session/clients 的地方) │
└───────────────────────────────┬──────────────────────────────────┘
                                ↓ 只依赖 core + clients
┌──────────────────────────────────────────────────────────────────┐
│ L3  clients/           客户端层(按域拆分的 API 调用)              │
│     base / profile_client / search_client / message_client        │
└───────────────┬──────────────────────────────┬───────────────────┘
                ↓                              ↓
   ┌────────────────────────────┐  ┌──────────────────────────────────┐
   │ L2  session/               │  │ L2  routes/  (纯数据,无下层依赖)  │
   │   state / extractor /      │  │   base / rest / graphql           │
   │   manager                  │  │   templates/  ← variables 模板     │
   └─────────────┬──────────────┘  └──────────────────────────────────┘
                 ↓
┌──────────────────────────────────────────────────────────────────┐
│ L1  transport/        传输层(抽象 + 多实现)                        │
│     base(协议) / http(curl_cffi) / cdp(DevTools) /                 │
│     factory(选择器) / proxy(代理解析) / browsing(浏览器能力识别)    │
└───────────────────────────────┬──────────────────────────────────┘
                                ↓ 只依赖 core
┌──────────────────────────────────────────────────────────────────┐
│ L0  core/             基础设施层(零内部依赖)                       │
│     config / exceptions / logger / extract / models               │
└──────────────────────────────────────────────────────────────────┘
```

### 依赖规则(与代码一致)

1. 依赖方向**只能向下**,禁止上层被下层 import。用脚本验证(退出码 0 即通过):

   ```bash
   .venv/bin/python tools/check_layers.py
   # upward deps: 0
   # import cycles: 0
   ```

2. `core` **只能 import 自身的子模块**,不得 import `transport` 及以上的任何层。

3. **同层允许「单向引用 + 组合根」,但禁止循环依赖**:
   - 允许:`clients/message_client.py` → `clients/profile_client.py`(用其解析用户名)
   - 允许:`services/factory.py` 装配所有 service;**不允许** `post_service` ↔ `search_service` 互相引用
   - 允许:`transport/factory.py` 引用 `http` / `cdp` 具体实现

4. 跨层传递**领域数据**时使用 `core/models` 中的 pydantic 模型;
   **协议原始载荷**(GraphQL JSON、HTML)在 client 内部以 `dict` 处理,不强行建模。

---

## 4. 目录结构(实际)

```
facebook-mcp/
├── ARCHITECTURE.md                  # 本文档(中文)
├── ARCHITECTURE.en.md               # 同一文档的英文版
├── README.md
├── CONTRIBUTING.md                  # 提交规范 / 文档隐私 / PR 要求
├── CHANGELOG.md                     # Keep a Changelog
├── LICENSE                          # MIT
├── Makefile                         # make check / hooks / chrome …
├── .gitmessage                      # 提交模板
├── pyproject.toml                   # 依赖 + 入口 facebook-mcp = facebook_mcp.server:main
├── .env.example                     # 全部 FB_* 配置项模板
├── .github/workflows/ci.yml         # CI:lint / 多版本测试 / 打包
├── src/facebook_mcp/
│   ├── __init__.py                  # __version__ = "0.1.0"
│   ├── __main__.py                  # python -m facebook_mcp
│   ├── server.py                    # L5 MCP 工具注册
│   │
│   ├── core/                        # L0 基础设施
│   │   ├── config.py                #   Settings(FB_ 前缀环境变量)
│   │   ├── exceptions.py            #   异常体系(§7)
│   │   ├── accounts.py              #   凭据目录布局(~/.facebook-mcp/accounts/*)
│   │   ├── logger.py                #   日志(输出到 stderr,避免污染 stdio)
│   │   ├── extract.py               #   通用 HTML 内嵌 JSON 抽取(无 FB 领域知识)
│   │   └── models/
│   │       ├── profile.py           #   Profile / Timeline
│   │       ├── post.py              #   Post / PostTimeResult
│   │       ├── search.py            #   SearchCategory / SearchResult
│   │       └── message.py           #   SendResult
│   │
│   ├── transport/                   # L1 传输层
│   │   ├── base.py                  #   Transport 协议 + HttpRequest/HttpResponse
│   │   ├── http.py                  #   CurlCffiTransport
│   │   ├── cdp.py                   #   CdpTransport(WebSocket 直连 DevTools)
│   │   ├── proxy.py                 #   显式 → 环境变量 → macOS 系统代理解析
│   │   ├── browsing.py              #   cdp_from():识别"能开浏览器"的传输
│   │   └── factory.py               #   get_transport() + HybridTransport
│   │
│   ├── session/                     # L2 会话层
│   │   ├── state.py                 #   SessionState(cookies + tokens + Page 身份探测)
│   │   ├── extractor.py             #   cookie 字符串 / Netscape 文件 / 页面 token 解析
│   │   └── manager.py               #   SessionManager:装载/require_auth/轮换/浏览器引导
│   │
│   ├── routes/                      # L2 路由注册表(纯数据)
│   │   ├── base.py                  #   Route 数据类 + FACEBOOK_BASE / GRAPHQL_PATH
│   │   ├── rest.py                  #   页面入口(profile / search 页面 URL)
│   │   ├── graphql.py               #   doc_id + fb_api_req_friendly_name 注册表
│   │   └── templates/
│   │       ├── __init__.py          #   build_search_variables():模板 → 具体 variables
│   │       └── search_results.json  #   抓包得到的搜索 variables 模板(42 个字段)
│   │
│   ├── clients/                     # L3 客户端层
│   │   ├── base.py                  #   BaseClient(请求、异常翻译、GraphQL 表单)
│   │   ├── profile_client.py        #   主页 HTML → Profile + Timeline
│   │   ├── search_client.py         #   GraphQL 搜索 → SearchResult
│   │   └── message_client.py        #   CDP 驱动 Messenger UI 发送
│   │
│   └── services/                    # L4 服务层
│       ├── factory.py               #   Services 组合根 + get_services() 单例
│       ├── post_service.py          #   get_latest_post_time / get_recent_posts
│       ├── search_service.py        #   search_people / search_pages
│       └── message_service.py       #   send_message(异常 → 结构化结果)
│
├── tools/                           # 逆向/运维辅助脚本(不参与运行时)
│   ├── probe.py                     # 单服务探测/人工验证
│   ├── sniff.py                     # CDP 抓包 → doc_id / variables;--save-templates 写 doc_ids 到账户库
│   ├── export_cookies.py            # CDP → 浏览器 cookie → Netscape 文件
│   ├── import_cookies.py            # 直接解密本地 Chrome profile 的 Cookies
│   ├── check_layers.py              # 校验分层依赖与循环引用
│   ├── smoke_mcp.py                 # MCP stdio 握手 + tools/list 冒烟测试
│   ├── commit_msg.py                # Conventional Commits 校验(git hook + CI)
│   └── launch_chrome_debug.sh       # 按账号隔离启动带调试端口的 Chrome
│
└── tests/                           # 211 项测试(全部离线)
    ├── conftest.py                  # 把 FB_HOME 指向临时目录,隔离真实凭据
    ├── fixtures/                    # profile_page.html / search_response.json
    ├── test_skeleton.py             # 工具注册与 schema
    ├── test_transport.py            # 本地 HTTP 服务器:重试/节流/错误映射
    ├── test_accounts.py             # 账户库:名称校验、权限、解析优先级、多账号
    ├── test_m2_profile.py           # 抽取、session、路由、主页解析
    ├── test_m3_search.py            # variables 模板、结果解析、jazoest
    ├── test_m4_cdp.py               # CDP 协议、hybrid 分流、cookie 引导
    ├── test_m4_cdp_integration.py   # 真实 mock DevTools 服务器(HTTP + WebSocket)
    ├── test_m5_message.py           # 发送流程、回退链、收件人解析、Page 身份拦截
    ├── test_route_doc_ids.py        # doc_id 覆盖、错误信封、模板提升
    ├── test_smoke_mcp.py            # smoke 客户端的管道/EOF 行为
    ├── test_logged_out_matrix.py    # 无痕(零 cookie)基线结论的回归测试
    └── test_commit_msg.py           # 提交信息规范校验器自身
```

---

## 5. 模块职责明细

### 5.1 L0 core

- `config.py` —— `Settings`(pydantic-settings,环境变量前缀 **`FB_`**):

  | 字段 | 环境变量 | 默认值 |
  |---|---|---|
  | `transport_mode` | `FB_TRANSPORT_MODE` | `auto` |
  | `impersonate` | `FB_IMPERSONATE` | `chrome` |
  | `request_timeout` | `FB_REQUEST_TIMEOUT` | `30.0` |
  | `max_retries` | `FB_MAX_RETRIES` | `3` |
  | `min_request_interval` | `FB_MIN_REQUEST_INTERVAL` | `2.0` |
  | `account` | `FB_ACCOUNT` | `default` |
  | `home` | `FB_HOME` | `~/.facebook-mcp` |
  | `cookie` | `FB_COOKIE` | – |
  | `cookie_file` | `FB_COOKIE_FILE` | – |
  | `user_agent` | `FB_USER_AGENT` | – |
  | `proxy` | `FB_PROXY` | – |
  | `proxy_from_system` | `FB_PROXY_FROM_SYSTEM` | `true` |
  | `cdp_url` | `FB_CDP_URL` | `http://127.0.0.1:9222` |
  | `cdp_page_url` | `FB_CDP_PAGE_URL` | `https://www.facebook.com/` |
  | `cdp_timeout` | `FB_CDP_TIMEOUT` | `60.0` |
  | `cdp_cookie_bootstrap` | `FB_CDP_COOKIE_BOOTSTRAP` | `true` |
  | `route_doc_ids` | `FB_ROUTE_DOC_IDS` | `{}`(通常由 `account.json` 的 `doc_ids` 注入,§5.4) |

- `exceptions.py` —— 异常体系(§7)。
- `accounts.py` —— 凭据存储布局(§5.8):`AccountPaths`、名称校验、0600/0700 权限、账户列表。
- `logger.py` —— 日志一律写 **stderr**(stdout 留给 MCP JSON-RPC)。
- `extract.py` —— 纯工具:`iter_json_scripts` / `walk` / `find_first` / `find_all` / `dig`。
  **刻意不包含 Facebook 领域知识**,领域解析留在 client 层。
- `models/` —— 领域模型,纯数据。

### 5.2 L1 transport

`base.py` 定义协议与数据结构:

```python
@dataclass(frozen=True)
class HttpRequest:
    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    params: dict[str, str] | None = None          # query string
    data: str | dict[str, str] | None = None      # form body
    json: Any | None = None                       # JSON body
    impersonate: str | None = None                # per-request TLS 指纹覆盖
    transport_hint: Literal["http", "cdp"] | None = None   # hybrid 分流依据

@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    url: str
    headers: dict[str, str]
    text: str
    content: bytes
    # .ok / .json() / .raise_for_status()
    # .json() 会剥掉 Facebook 的 `for (;;);` 防 JSON 劫持前缀(GraphQL 错误信封带它)

class Transport(Protocol):
    name: str
    def request(self, req: HttpRequest) -> HttpResponse: ...
    def close(self) -> None: ...
```

- `http.py` —— `CurlCffiTransport`:固定 impersonate、指数退避重试
  (连接错误 + `429/500/502/503/504`,遵循 `Retry-After`)、最小请求间隔节流、
  **回环地址自动绕过代理**。
- `cdp.py` —— `CdpTransport`:
  - `GET /json/version`、`/json/list` 发现页面;无可用页面时 `PUT /json/new` 建页
  - WebSocket 握手用 `suppress_origin=True`(规避 Chrome 对 `Origin` 的 403)
  - `request()` 在页面主世界执行 `fetch()`,浏览器自带 cookie / UA / TLS 指纹;
    自动剔除脚本不可设置的受限头(`Cookie`/`User-Agent`/`Sec-*`/`Host` 等)
  - `cookies()` / `evaluate()` / `navigate()` / `send()` / `drain_events()`
  - **事件缓冲**:等待命令回复期间收到的 CDP 事件不丢弃(抓包必需)
- `proxy.py` —— 三级代理解析:显式 `FB_PROXY` → `HTTPS_PROXY` 等环境变量 → macOS `scutil --proxy`。
- `browsing.py` —— `cdp_from(transport)`:结构化判定传输是否具备浏览器能力
  (直接 `CdpTransport`,或 `HybridTransport` 的 `.cdp` 半边)。
- `factory.py` —— `get_transport()` 与 `HybridTransport`(按 `transport_hint` 分流)。

> **隐式契约**:`base.py` 的 `_DEFAULT_HEADERS` 固定携带
> `Accept: text/html,application/xhtml+xml,…`。
> 不带这个头时,Facebook 会返回"精简版"主页(没有 `timeline_list_feed_units`),
> 抓取器会静默得到 0 条帖子。`tests/test_logged_out_matrix.py` 锁住了这一行为。

### 5.3 L2 session

- `state.py` —— `SessionState`:`cookies` 字典 + `fb_dtsg` + `lsd` + `user_agent`;
  提供 `is_authenticated`(需 `c_user` + `xs`)、`cookie_header()`、`as_headers()`、`merge_tokens()`,
  以及身份探测 `impersonator_id` / `acting_as_page`(`i_user` 存在 ⇒ 当前是 Page 身份,§5.5)。
- `extractor.py` —— `from_cookie_string()`(Netscape 头字符串)、`from_cookie_file()`
  (Netscape `cookies.txt`)、`tokens_from_html()`(`DTSGInitialData` / `LSD`)、`user_id_from_html()`。
- `manager.py` —— `SessionManager`:
  - 装载优先级:`FB_COOKIE_FILE` → `~/.facebook-mcp/accounts/<account>/cookies.txt`
    → `FB_COOKIE` → 匿名(详见 §5.8)
  - `require_auth()`:未登录时**尝试**通过 `cookie_provider` 从浏览器补齐,再失败才抛 `AuthError`
  - `learn_from_html()`:从页面刷新 `lsd` / `fb_dtsg`
  - `from_config`:cookie 是否来自配置或账户库(而非浏览器),决定是否走纯协议(见 §9.3)

> cookie 从浏览器提取的实际路径是 `SessionManager(cookie_provider=cdp.cookies)`,
> 由 `services/factory.py` 注入;`extractor.py` 本身不接触 CDP。

### 5.4 L2 routes(逆向产物的落点)

本层是**纯数据 + 数据变换**,不依赖任何其它层(只有 `base.py` 定义数据类)。

- `base.py` —— `Route(op, method, path, doc_id, friendly_name, requires_auth, anonymous)`。
  `anonymous=True` 表示**不带会话 cookie** 请求:主页就是这种情况 ——
  带 cookie 时 Facebook 返回登录态 SPA 外壳(`userVanity` / `timeline_list_feed_units`
  全部消失),只有未登录变体才内嵌 ServerJS 载荷。
- `rest.py` —— 页面入口:`PROFILE_PAGE`(`/{username}`)被 `profile_client` 使用;
  `SEARCH_PEOPLE_PAGE` / `SEARCH_PAGES_PAGE` **仅作页面入口记录**(搜索实际走 GraphQL,
  当前无生产代码调用,保留用于导航与诊断)。
- `graphql.py` —— `doc_id` 注册表:

  | 路由常量 | `doc_id` | `friendly_name` | 状态 |
  |---|---|---|---|
  | `PROFILE_HEADER` | `28585259111117102` | `ProfileCometHeaderQuery` | 已捕获,未使用 |
  | `PROFILE_TIMELINE_REFETCH` | – | `ProfileCometTimelineFeedRefetchQuery` | 未捕获(不需要) |
  | `SEARCH_RESULTS` | `39411546341825607` | `SearchCometResultsPaginatedResultsQuery` | ✅ 使用中 |
  | `SEND_MESSAGE` | – | `SendMessageMutation` | 不存在于页面级(见 §10 M5) |

- 表里的 `doc_id` 只是 **bootstrap 默认值**:`BaseClient._doc_id()` 优先读
  `account.json` 的 `doc_ids`(`{"search.results": "..."}`)。`doc_id` 随每次
  前端 build 轮换、且可能因账号而异,所以运行时值放在凭证存储里,
  刷新它不改源码、也不产生提交。
- ⚠️ 该覆盖在 **`Services` 构造时**读取一次并按账号缓存(`get_services(account)`
  为 `lru_cache`)。因此刷新 `account.json` 后需要**重启 MCP server**
  (或重启 MCP 客户端里的该 server 进程)才生效。

- `templates/` —— 搜索 `variables` 模板。包含 42 个字段(其中 27 个 Relay feature-gate),
  作为**数据**管理;`build_search_variables()` 只替换
  `args.text` / `args.experience.type` / `args.context.bsid` / `count` / `cursor`。

### 5.5 L3 clients

- `base.py` —— `BaseClient(transport, session, settings)`,提供:
  - `_request()` / `_get()`:合并默认头 + session 头;**决定 `transport_hint`**
  - `_graphql(route, variables)`:构造 Relay 表单,含 `fb_dtsg` / `lsd` /
    **`jazoest`(由 `fb_dtsg` 推导)**
  - `_ensure_dtsg()`:缺 token 时自动拉首页补齐
  - `_raise_for_facebook_status()` / `_raise_for_graphql_errors()`:状态码与 Relay
    `errors` → 项目异常
- `profile_client.py` —— **单次 GET 主页 HTML** 完成两件事:
  - `_parse_profile()`:`userVanity` 锚定 `userID`(`actors` id 匹配取显示名)
  - `_parse_posts()`:解析 `timeline_list_feed_units.edges[*].node`
  - 公开 API:`fetch_timeline(username) -> Timeline`、`resolve(username) -> Profile`
  - `Timeline.payload_complete`:为 `False` 时表示页面根本没有 timeline 段
    (Facebook 的"精简版"),此时"0 条帖子"并不代表用户没有发帖
  - **不使用 GraphQL**,也**不使用 cookie**:`PROFILE_PAGE.anonymous=True`,
    请求时跳过会话头。带 cookie 的登录态页面是客户端渲染的空壳,解析会得到 0 条帖子
- `search_client.py` —— `search(query, category, limit)`:
  - `build_search_variables()` → `_graphql(SEARCH_RESULTS, ...)`
  - `_parse_results()`:读 `data.serpResponse.results.edges[*].rendering_strategy.view_model.profile`,
    去重、跳过畸形节点、按 `__typename` 区分 Page/User
  - `next_cursor()`:读取 `page_info.end_cursor`(分页预留)
  - **不解析搜索结果页面 HTML**(结果由客户端渲染,初始 HTML 中没有)
- `message_client.py` —— `send(recipient, text)`:
  - `resolve_recipient()`:uid / username / `@vanity` / `facebook.com/<vanity>` / `profile.php?id=`
  - `_open_thread()`:**三级回退** `直接线程 URL` → `会话列表点击` → `主页 Message 按钮`
    (从未聊过的对象必须走第三级;按钮需用真实鼠标事件 `Input.dispatchMouseEvent`,
    JS `.click()` 触发不到 React handler)
  - **Page 身份下第三级不可用**:Facebook 不给 Page actor 渲染 profile 的
    `Message` 按钮(Page 不能主动给陌生人发起会话)。判定方式:导出 cookie
    判断依据是 `i_user` cookie:**实时 `Network.getCookies` 非空时以浏览器为准**
    (浏览器是权威,快照陈旧也不会误判);只有实时读取失败或返回空时才退回导出快照
    (`SessionState.acting_as_page`)。
    此时第二级失败就直接报 `UnsupportedError` 并说明原因,不再白等 35s
    轮询一个永不出现的按钮。已有的会话仍可回复(第一、二级不受影响)
  - 输入框识别要求 `aria-label` 匹配 `^Write to .+`(名字非空),
    以排除会话列表里那个 `Write to ` 的新建会话搜索框
  - `_compose_and_send()`:聚焦 → `Input.insertText` → **校验输入框已接收文本**
    → 点 `Press enter to send`(回退 Enter)→ **轮询输入框清空**作为送达判据
  - `send_via_graphql()`:保留的协议化入口,**当前显式抛 `UnsupportedError`** 并说明原因

### 5.6 L4 services

薄编排:参数校验、client 调用、异常 → 结构化结果。

- `PostService.get_latest_post_time(username) -> PostTimeResult`
- `PostService.get_recent_posts(username, limit) -> list[Post]`
- `SearchService.search_people(q, limit)` / `search_pages(q, limit) -> list[SearchResult]`
- `MessageService.send_message(recipient, text) -> SendResult`(**不抛异常**,失败写 `error`)
- `factory.Services` —— 唯一装配点:settings → transport → session → clients → services;
  `get_services(account)` **按账号缓存**实例(每个账号独立 transport + session);
  `build_settings(account)` 会应用 `account.json` 里的 `cdp_url` 等覆盖项;
  auto 模式下把 `transport.cdp.cookies` 注入 session。

### 5.7 L5 server

`mcp.server.fastmcp.FastMCP` 注册 **5 个工具**,输入/输出均为 pydantic 模型:

| 工具 | 签名 | 返回 |
|---|---|---|
| `get_user_latest_post_time` | `(username: str, account: str \| None = None)` | `PostTimeResult` |
| `get_user_recent_posts` | `(username: str, limit: int = 10, account: ...)` | `list[Post]` |
| `search_people` | `(q: str, limit: int = 10, account: ...)` | `list[SearchResult]` |
| `search_pages` | `(q: str, limit: int = 10, account: ...)` | `list[SearchResult]` |
| `send_message` | `(recipient: str, text: str, account: ...)` | `SendResult` |

`account` 恒为可选;不传时使用 `FB_ACCOUNT`。`tools/smoke_mcp.py` 会在 CI 中校验
"每个工具都有 `account` 且非必填"。

> SDK 的 `FastMCP` 不接受 `version` 参数,`server.py` 在构造后改写
> `_mcp_server.version`,使 `initialize` 返回项目版本 `0.1.0`。

### 5.8 L0 凭据隔离:账户库

凭据**从不进入仓库**,统一放在用户主目录下,按账号分目录:

```
~/.facebook-mcp/                     # 0700
└── accounts/
    ├── default/                     # 0700
    │   ├── cookies.txt              # 0600,Netscape 格式
    │   └── account.json             # 0600,元数据 + 每账号覆盖项
    └── work/
        ├── cookies.txt
        └── account.json
```

**cookie 解析优先级**(`SessionManager._load`):

| 顺序 | 来源 | 说明 |
|---|---|---|
| 1 | `FB_COOKIE_FILE` | 显式路径,最高优先级(容器/临时调试) |
| 2 | `~/.facebook-mcp/accounts/<FB_ACCOUNT>/cookies.txt` | **默认**,隔离且可持久 |
| 3 | `FB_COOKIE` | 内联字符串;会出现在 `ps` / shell 历史中,仅作兜底 |

无任何来源时以匿名运行(仅公开页可用)。

**`account.json` 支持的每账号覆盖项**:

| 键 | 作用 |
|---|---|
| `c_user` / `authenticated` / `updated_at` | 导出时写入的元数据,用于 `--list` 诊断 |
| `cdp_url` | 该账号绑定**自己的** Chrome 实例(独立 profile + 端口) |
| `doc_ids` | `{"<route.op>": "<doc_id>"}`:刷新随 build 轮换的 GraphQL `doc_id`,无需改源码(§5.4) |
| `cdp_page_url` / `user_agent` / `impersonate` | 同上,覆盖全局设置 |

**哪些操作真的需要 cookie**

| 操作 | 需要 cookie | 原因 |
|---|---|---|
| `get_user_latest_post_time` / `get_user_recent_posts` | **否** | 主页 HTML 公开可读 |
| `search_people` / `search_pages` | **是** | 未登录直接 404;GraphQL 需 `fb_dtsg` |
| `send_message` | **否(但需要浏览器)** | 发送使用 Chrome 自身的登录态,不读本项目的 cookie 文件 |

**权限防护**

- 目录 `0700`、文件 `0600`,写入时 `chmod`;
- 读取时若 `cookies.txt` 对他人可读,记录 `WARNING` 并提示 `chmod 600`;
- 账户名经正则校验(`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`),阻止 `../` 路径穿越。

### 5.9 多账号

两级方案,按需选用:

**L1 — 进程级切换(默认)**

一个 MCP server 进程 = 一个活跃账号,由 `FB_ACCOUNT` 决定。
多账号即启动多个进程,每个用不同 env —— 完全隔离,无并发问题:

```json
{
  "mcpServers": {
    "facebook-default": {
      "command": ".../facebook-mcp",
      "env": { "FB_ACCOUNT": "default", "FB_TRANSPORT_MODE": "http" }
    },
    "facebook-work": {
      "command": ".../facebook-mcp",
      "env": { "FB_ACCOUNT": "work", "FB_TRANSPORT_MODE": "http" }
    }
  }
}
```

**L2 — 调用级切换(已实现)**

同一进程内按调用选择账号:所有 MCP 工具都多了可选参数 `account`,
`get_services(account)` 按账号名缓存一个 `Services`(独立 transport + session)。

```
搜索: search_pages(q="pest control", account="work")
默认: search_pages(q="pest control")            # 用 FB_ACCOUNT
```

并发注意:同一账号若同时被多个并发调用命中,共用同一个 curl_cffi Session;
**发送消息**会独占浏览器页面,不同账号必须绑定不同的 `cdp_url`(见 `account.json`),
否则会互相抢占同一个 Chrome 标签页。

**按账号隔离的浏览器实例**(`tools/launch_chrome_debug.sh`):

```bash
tools/launch_chrome_debug.sh --account default          # 端口 9222
tools/launch_chrome_debug.sh --account work             # 端口自动 9223
tools/launch_chrome_debug.sh --account work --seed      # 首次:从主 Chrome 移植 cookie
tools/launch_chrome_debug.sh --account work --stop      # 只关这个账号的实例
```

| 行为 | 说明 |
|---|---|
| profile 位置 | `~/.facebook-mcp/accounts/<account>/chrome`(0700) |
| 端口 | 未指定时按账号顺序推导(`9222`、`9223`…),并写回 `account.json` 的 `cdp_url` |
| 主 Chrome | **从不触碰**,不需要退出用户的日常浏览器 |
| `--seed` | 只复制 `Cookies` + `Local State`(约 3.6 MB),不带整个 13 GB profile |
| `--stop` | 按 `--user-data-dir` 精确匹配进程,不影响其它账号 |

---

## 6. 数据流(实际路径)

### 6.1 获取最新帖子时间(公开,零 cookie)

```
server.get_user_latest_post_time("exampleuser")
  → PostService.get_latest_post_time("exampleuser")
    → ProfileClient.fetch_timeline("exampleuser")
      → GET https://www.facebook.com/exampleuser          # curl_cffi,无需登录
      → iter_json_scripts(html)                            # 84 个 data-sjs JSON 块
      → _parse_profile: userVanity + userID → Profile(uid=100000000000001)
      → _parse_posts:   timeline_list_feed_units.edges[].node
                       → Post(id, text, created_at, url, author_uid)
    → Timeline.latest_post(按 created_at 取最大)
  → PostTimeResult(ok, latest_post_time=2026-09-30T06:53:54Z, post_url, post_text)
```

### 6.2 关键词搜索(需登录,纯协议)

```
server.search_pages("pest control")
  → SearchService.search_pages(q, limit)
    → SearchClient.search(q, SearchCategory.PAGES, limit)
      → build_search_variables(q, "pages", limit)          # 模板 + 替换 5 个字段
      → BaseClient._graphql(SEARCH_RESULTS, variables)
          → SessionManager.require_auth()                  # 无 cookie 时先从浏览器引导
          → _ensure_dtsg()                                 # 缺 fb_dtsg 时拉首页补齐
          → POST /api/graphql/  doc_id=39411546341825607
               fb_dtsg / lsd / jazoest / variables
      → _parse_results(payload) → list[SearchResult]
      → payload 含 errors → ParseError / AuthError(见 §7)
```

`transport_hint` 规则决定这一跳走 curl_cffi 还是浏览器(见 §9.3)。

### 6.3 发送消息(需登录 + 浏览器)

```
server.send_message(recipient="exampleuser", text="...")
  → MessageService.send_message(...)
    → MessageClient.send(recipient, text)
      → resolve_recipient(): username → uid(需要 ProfileClient)
      → cdp_from(transport)                                # 必须是浏览器通道
      → navigate  https://www.facebook.com/messages/t/<uid>/
      → 等 composer;失败则点击会话列表中的该会话
      → 仍失败:Page 身份直接报错(Page 无法主动发起会话,§5.5)
      → 否则打开主页点 "Message"
      → Input.insertText → 校验输入框已接收
      → 点 "Press enter to send"(回退:Enter 键)
      → 轮询输入框是否清空
  → SendResult(ok=True, thread_id=...)
```

> 这里没有 GraphQL:发送由 Facebook 的 service worker 发起,详情见 §10「M5」与 §8.3。

---

## 7. 异常体系

```
FacebookError                    core/exceptions.py —— 所有项目异常的基类
├── AuthError                    登录态缺失/失效(fb_dtsg 失效、被要求登录)
├── RateLimitError               频率限制(可带 retry_after)
├── ChallengeError               验证码/风控检查 —— 已定义,当前代码路径未触发
├── NotFoundError                用户/主页/线程不存在(HTTP 404)
├── TransportError               传输层失败
│   ├── HttpStatusError          非 2xx(4xx/5xx,不含 404/403/401/429)
│   ├── ParseError               响应结构变化、GraphQL errors、模板过期
│   └── CdpError                 transport/cdp.py —— DevTools 协议调用失败
└── UnsupportedError             该**传输或账号类型**不具备此能力(无浏览器时发消息、Page 身份发起会话)

TemplateError (routes/templates) —— RuntimeError 子类:模板缺失/损坏/类别不支持
```

**翻译规则**

| 来源 | 条件 | 结果 |
|---|---|---|
| HTTP | `404` | `NotFoundError` |
| HTTP | `401` / `403` | `AuthError` |
| HTTP | `429` | `RateLimitError` |
| HTTP | 其他 `>= 400` | `HttpStatusError` |
| Relay `errors` | 含 `missing_required_variable` | `ParseError`(提示重新抓模板) |
| Relay `errors` | **未登录**时的 `rate limit`(code `1675004`) | `AuthError` |
| Relay `errors` | 含 `log in` / `login` / `permission` / `checkpoint` 等 | `AuthError` |
| Relay `errors` | 其他 | `ParseError` |
| 响应体 | 不是 JSON(如 `for (;;);` 后仍是 HTML) | `ParseError`,提示 `doc_id` 可能已轮换 |
| 旧式信封 | `{"error": <code>, "errorSummary": ...}`(无 `errors`) | `ParseError`;`1357031`(content no longer available)会点名 `doc_id` 已轮换 |

**对 MCP 的表现**:`search_*` / `get_*` 抛异常交由 SDK 转成 `isError`;
`send_message` 由 `MessageService` 捕获并返回 `SendResult(ok=False, error=...)`。

---

## 8. 逆向工程工作流与实测结论

### 8.1 实测侦察结论(2026-10)

先给出**无痕(incognito)基线** —— 全新 Chrome 实例 + 空 `--user-data-dir`,
`Network.clearBrowserCookies` 后每个场景独立测试,即"零 Facebook cookie"的真实访客。

| 场景 | 无痕结果 | 已登录结果 | 是否需要 cookie |
|---|---|---|---|
| `GET /<username>`(带 `Accept: text/html`) | **200,963 KB,含 timeline** | 200 | **否** |
| `GET /<username>`(**不带** `Accept`) | 200,但仅 509 KB,**无 timeline** | – | 否,但**缺头会静默降级** |
| `GET /search/people?q=` | **404 `Not Found`** | 200 | **是** |
| `GET /search/pages?q=` | **404** | 200 | **是** |
| `GET /search/top?q=` | **404** | 200 | **是** |
| `POST /api/graphql/` 搜索(匿名) | 200 + `{"message":"Rate limit exceeded","code":1675004}` | 200 + 数据 | **是** |
| `POST /ajax/mercury/send_messages.php` | 404(已下线) | 404 | – |

> ⚠️ **匿名 GraphQL 会伪装成限流**:未登录时 Facebook 不返回鉴权错误,而是
> `Rate limit exceeded`(code `1675004`)。`BaseClient._raise_for_graphql_errors`
> 已区分:未登录 → `AuthError`(提示配置 cookie),已登录 → `RateLimitError`。

> ⚠️ **`Accept` 头是隐式契约**:不带 `text/html` 时 Facebook 返回"精简版"主页
> (无 `timeline_list_feed_units`),表现为"查不到帖子"而非报错。
> `_DEFAULT_HEADERS` 固定携带该头,`Timeline.payload_complete` 会标记
> "页面没有 timeline 段",`PostService` 在错误信息里提示重试。

**登录态会改变返回的 HTML 形态**(2026-10 实测,同一 URL 同一时刻):

| 请求方式 | 大小 | `userVanity` | `timeline_list_feed_units` | 结论 |
|---|---|---|---|---|
| 匿名 | 961 KB | 2 | 4 | 内嵌完整 ServerJS 载荷 |
| 带 `c_user`/`xs` | 1 181 KB | **0** | **0** | 登录态 SPA 外壳,数据靠 GraphQL 拉取 |

因此**主页必须匿名抓取**;这也是 `Route.anonymous` 存在的原因。
早期实现会带上 cookie,导致「配置了搜索凭据后取帖子时间反而失败」。

**结论**

| 操作 | 需要 cookie | 依据 |
|---|---|---|
| `get_user_latest_post_time` / `get_user_recent_posts` | **否** | 无痕下 200 且 timeline 完整 |
| `search_people` / `search_pages` | **是** | 无痕下 404;匿名 GraphQL 被拒 |
| `send_message` | **否,但需要浏览器登录** | 走 Chrome 自身会话 |

**主页 HTML 关键结构**(公开可读):

```
84 个 <script type="application/json" data-sjs> 块(均为合法 JSON)
├── "userVanity":"exampleuser" + "userID":"100000000000001"   → 身份解析锚点
├── "LSD",[],{"token":"..."}                                    → lsd token
└── ...data.<user|page>.timeline_list_feed_units.edges[0].node
      ├── post_id / creation_time(Unix 秒) / permalink_url
      ├── actors[0].{id,name}                                   → 作者
      └── comet_sections.content.story.message.text             → 正文
```

未登录时 `DTSGInitialData` 为 `{}`(空),`lsd` 仍有值。

**搜索结果结构**(GraphQL,已实测):

```
POST /api/graphql/  doc_id=39411546341825607
     fb_api_req_friendly_name=SearchCometResultsPaginatedResultsQuery
  → data.serpResponse.results.edges[*].rendering_strategy
        .view_model.profile.{id, name, url, is_verified, profile_picture.uri}
  → data.serpResponse.results.page_info.{has_next_page, end_cursor}
```

### 8.2 关键发现:`jazoest` 可推导

```python
jazoest = "2" + str(sum(ord(c) for c in fb_dtsg))   # 实测 25724 == 25724
```

因此**不需要浏览器计算签名**。`fb_dtsg` 从页面 HTML 的 `DTSGInitialData` 取出即可。
这意味着搜索可完全用 curl_cffi 完成,浏览器只用于**一次性 cookie 引导**。

### 8.3 Messenger 发送路径(M5 逆向结论)

| 假设 | 验证 | 结论 |
|---|---|---|
| 页面级 GraphQL mutation | 抓 `Network.requestWillBeSent` | ❌ 仅 `FBScreenTimeLogger_syncMutation` |
| Lightspeed WebSocket | 抓 `Network.webSocketFrameSent` 并 base64 解码 | ❌ 二进制帧内**不含**消息正文 |
| `Fetch` 域可拦截 | 页面目标 `Fetch.enable` + `*graphql*` | ❌ 拦不到 —— 由 **Service Worker** 发起 |
| legacy REST 端点 | `POST /ajax/mercury/send_messages.php` | ❌ 404 |
| 线索 | `bootloader-endpoint` 加载 `LSBumpThread` / `LSReplaceOptimisticMessage` | 走 Lightspeed 同步 |

→ 无法以低成本伪造协议,故 `send_message` 采用 **CDP 驱动真实 UI**。

**actor 类型会影响 UI**:以 Page 身份操作时,Facebook 进入 page inbox 而不是个人
Messenger,并且**不在别人的 profile 上渲染 `Message` 按钮**(Page 不能主动发起会话)。
因此第 3 级回退只对个人账号有效;Page 账号的判定见 §5.5 的 `acting_as_page`。

### 8.4 Chrome 远程调试的两个坑(已在代码中解决)

1. **Chrome 136+ 禁止默认 profile 开启远程调试** —— 必须显式 `--user-data-dir`。
   本项目为每个账号使用 `~/.facebook-mcp/accounts/<account>/chrome`,
   首次可用 `--seed` 移植 `Cookies` / `Local State`(约 3.6 MB)保留登录态,
   避免复制 13 GB 的主 profile(§5.9)。
2. **DevTools WebSocket 拒绝带 `Origin` 头的握手**(403)——
   客户端用 `suppress_origin=True`,不必放宽 Chrome 的 `--remote-allow-origins`。

### 8.5 通道选择(按成本递减)

| 优先级 | 通道 | 用途 |
|---|---|---|
| 1 | `curl_cffi` | 公开页、GraphQL 读接口 |
| 2 | CDP `Network` 域 | 发现真实端点 / `doc_id` / `variables` |
| 3 | CDP 页面执行 / UI 驱动 | 需要登录态或 service worker 场景 |
| 4 | JSVMP 逆向(未启用) | 仅在出现签名类风控时 |

### 8.6 Facebook 改版后的维护流程

1. 找到出错的工具,从 `ParseError` 的信息判断原因:
   - `doc_id ... was rejected` / `Facebook error 1357031` → **`doc_id` 轮换**(最常见);
   - `...template are stale` → `variables` 模板过期;
   - `composer did not appear` 之类 → DOM 布局变化。
2. 用 `tools/sniff.py` 重新抓包:

   ```bash
   .venv/bin/python tools/sniff.py \
       --navigate "https://www.facebook.com/search/pages?q=pest%20control" \
       --seconds 20 --save-templates
   ```

3. `--save-templates` 会把抓到的 `doc_id` 写进
   `~/.facebook-mcp/accounts/<account>/account.json` 的 `doc_ids`,
   并(仅限 `PROMOTED_TEMPLATES` 白名单)重写
   `routes/templates/search_results.json`;其余抓包结果只留在 gitignored 的
   `.cache/routes.json`,避免死模板(及其中的会话 id)进 wheel。
4. 正常情况下到此为止 —— 不需要改 `routes/graphql.py`。
   **重启 MCP server** 让新的 `doc_ids` 生效(§5.4)。
5. 只有想把“新装即用”的 bootstrap 默认值推上去时,才把抓到的 `doc_id` 提交进
   `Route(...)`。`tools/sniff.py` 打印的 route 表就是给这一步的,平时不必抄进源码。
6. 仅动 `routes/`,不改 client / service 逻辑。

---

## 9. 关键设计决策

### 9.1 传输层抽象是第一公民

`Transport` 协议是唯一契约;`CurlCffiTransport` / `CdpTransport` / `HybridTransport`
都实现它。上层 client 完全不感知请求物理上如何发出。

### 9.2 routes 与 clients 分离

接口定义(`doc_id`、端点、`variables` 模板)是**数据**,随 Facebook 改版频繁变化;
client 是**逻辑**。改版只改 `routes/`,业务代码不动。§5.4 的 `account.json → doc_ids`
覆盖层更进一步:仅 `doc_id` 轮换时连 `routes/` 都不用改。见 §8.6。

### 9.3 协议优先的传输选择

由 `BaseClient` 决定 `transport_hint`:

| 条件 | 通道 |
|---|---|
| 公开读(`requires_auth=False`) | curl_cffi |
| 需登录 **且** cookie 来自账户库/配置(`session.from_config`) | **curl_cffi(纯协议)** |
| 需登录 **且** 无配置 cookie | CDP(浏览器代跑,首次自动取 cookie) |
| 发送消息 | 强制 CDP(`cdp_from()`),HTTP-only 时抛 `UnsupportedError` |

### 9.4 会话生命周期独立成层

cookie / token 的装载、校验、刷新、浏览器引导集中在 `SessionManager`,
避免每个 client 各自处理登录态。

### 9.5 服务层 = 用例,客户端层 = 能力

LLM 调的是 `search_pages(q)`,不面对 `doc_id` / `variables`;
MCP 层只做转发,不含业务逻辑。

---

## 10. 实施里程碑

| 阶段 | 内容 | 状态 |
|---|---|---|
| M0 | 脚手架:pyproject、venv(3.11)、分层目录、core 模型/配置/异常 | ✅ |
| M1 | transport 层:base + curl_cffi + proxy + 重试/节流 + 单元测试 | ✅ |
| M2 | session + routes + profile_client + post_service | ✅ 真机验证 |
| M3 | search_client + search_service + MCP 工具 + variables 模板 | ✅ 真机验证(纯协议) |
| M4 | CDP 传输层:发现/连接/执行/cookie 引导/事件缓冲/抓包工具 | ✅ 真机验证 |
| M5 | message_client + message_service + `send_message` | ✅ 真机验证(浏览器路径) |
| M6 | 测试(211 项)、README、可编辑安装、CI、MIT License | ✅;PyPI 发布未做 |

### M1 交付明细

- `transport/base.py`:`Transport` 协议 + `HttpRequest` / `HttpResponse`
- `transport/http.py`:`CurlCffiTransport`(impersonate、退避重试、节流、回环绕过代理)
- `transport/proxy.py`:三级代理解析
- `transport/factory.py`:`get_transport()`

> **环境实测**:本机无直连 DNS,但系统代理 `127.0.0.1:10808` 可用,传输层自动识别。

### M2 交付明细

- `core/extract.py`、`session/{state,extractor,manager}.py`
- `routes/{base,rest,graphql}.py`
- `clients/base.py` + `clients/profile_client.py`
- `services/post_service.py`、`services/factory.py`

### M3 交付明细

- `routes/graphql.py`:`SEARCH_RESULTS`(`doc_id=39411546341825607`)
- `routes/templates/search_results.json`:抓包得到的 `variables` 模板
- `routes/templates/__init__.py`:`build_search_variables()`
- `clients/search_client.py`、`services/search_service.py`
- `clients/base.py`:`jazoest` 推导、`_ensure_dtsg()`、Relay 错误翻译
- `tools/sniff.py`(`--navigate` / `--save-templates`)、`tools/export_cookies.py`

### M4 交付明细

- `transport/cdp.py`:`CdpTransport`(目标发现、`suppress_origin`、页面内 `fetch`、
  `cookies` / `evaluate` / `navigate` / `drain_events`、事件缓冲)
- `transport/factory.py`:`HybridTransport`(按 `transport_hint` 分流)
- `transport/browsing.py`:`cdp_from()`
- `session/manager.py`:`cookie_provider` + `from_config`
- `services/factory.py`:把 CDP cookie 能力注入 session(懒加载)
- `tools/launch_chrome_debug.sh`、`tools/import_cookies.py`

### M5 交付明细

- `clients/message_client.py`:收件人解析、线程导航(含回退)、输入校验、发送、送达判据
- `services/message_service.py`:异常 → `SendResult(ok=False, error=...)`
- 逆向证据与备选方案见 §8.3;`send_via_graphql()` 为保留入口,当前显式 `UnsupportedError`

### M6 交付明细

- 测试:211 项,覆盖 12 个文件(含 mock DevTools 服务器的 CDP 集成测试)
- 分层依赖校验:`tools/check_layers.py` 输出 `upward deps: 0` / `import cycles: 0`
- 文档:`ARCHITECTURE.md`(中)/ `ARCHITECTURE.en.md`(英)/ `README.md` / `CONTRIBUTING.md`
- CI:`.github/workflows/ci.yml`(lint / 多版本测试 / 打包校验)
- `Makefile`:`make check` 本地等价于 CI;`make hooks` 安装 commit-msg 钩子
- `CONTRIBUTING.md` + `.gitmessage` + `LICENSE`(MIT)
- 安装:`pip install -e ".[dev]"` 可用;未发布到 PyPI

---

## 11. 风险与合规

- **风控**:Facebook 对批量搜索/私信有严格频率限制与行为检测;
  需限速(`min_request_interval`)、避免高频新账号操作。
- **改版**:`doc_id`、`variables` 模板、HTML 结构都会变化。`doc_id` 已被
  `account.json` 覆盖层吸收(仅它轮换时不动源码),其余通过 `tools/sniff.py`
  + `routes/` 快速迭代(§8.6)。
- **发送路径脆弱性**:`send_message` 依赖 Messenger DOM(`contenteditable`、
  `aria-label="Press enter to send"`)。布局变化会导致 `ParseError`,需要更新选择器。
- **合规**:仅用于授权账号;发送营销消息需遵守 Facebook 平台政策与
  反垃圾法规(如 CAN-SPAM、GDPR)。

---

## 12. 依赖约束

| 包 | 版本 | 说明 |
|---|---|---|
| `mcp` | `>=1.2,<2` | **官方 MCP Python SDK**,服务端使用 `mcp.server.fastmcp.FastMCP` |
| `curl_cffi` | `>=0.9` | 浏览器 TLS/JA3 指纹模拟 |
| `pydantic` / `pydantic-settings` | `>=2.10` / `>=2.6` | 模型与配置 |
| `beautifulsoup4` + `lxml` | `>=4.12` / `>=5.2` | HTML 内嵌 JSON 抽取 |
| `websocket-client` | `>=1.8` | CDP(Chrome DevTools)传输 |

`pip install -e ".[dev]"` 额外安装:

| 包 | 用途 |
|---|---|
| `pytest` | 测试运行 |
| `ruff` | lint + format(CI 强制) |
| `websockets` | `test_m4_cdp_integration.py` 的 mock DevTools 服务器 |
| `cryptography` | `tools/import_cookies.py` 解密 Chrome `v10` cookie |

> ⚠️ **不使用独立 `fastmcp` 包**,只用官方 `mcp<2` SDK 自带的 `FastMCP`。
> 若升级到 `mcp>=2`,需重新验证 `FastMCP` 的 `tool()` 签名与 `serverInfo.version` 改写逻辑。


---

## 13. 持续集成

`.github/workflows/ci.yml` 三个 job:

| Job | 内容 |
|---|---|
| `lint & architecture` | `ruff check` + `ruff format --check`、`tools/check_layers.py`、commit message 规范(PR)、凭据泄漏防护(禁止 `cookies.txt` / `account.json` / `.env` 入库) |
| `test` | `pytest` on Python 3.11 / 3.12 / 3.13(Ubuntu)+ 3.11(macOS);随后跑 `tools/smoke_mcp.py` |
| `build & install wheel` | `python -m build` → 断言 `routes/templates/*.json` 被打进 wheel → 在干净 venv 里安装并导入 |

**为什么 CI 不需要网络或凭据**:测试全部离线 ——
`tests/test_transport.py` 起本地 `http.server`,
`tests/test_m4_cdp_integration.py` 用 `websockets` 起一个 mock DevTools 服务器
(HTTP 发现端点 + WebSocket 协议),`tests/conftest.py` 把 `FB_HOME` 指向临时目录,
确保任何测试都读不到真实的 `~/.facebook-mcp`。

**本地等价**:

```bash
make check     # lint + fmt-check + layers + test + smoke
```

**代码规范**:`ruff`(line-length 100,target py311),规则集
`E/F/I/B/BLE/SIM/RUF/UP`;`tests/*` 放宽 `BLE001`(测试里故意捕获宽异常)。

**提交规范**:Conventional Commits,由 `tools/commit_msg.py` 校验 ——
本地经 `make hooks` 装成 `commit-msg` 钩子,CI 在 PR 上跑
`--range origin/<base>..HEAD`。类型 `feat fix docs test refactor perf build ci chore revert`,
scope 见 `CONTRIBUTING.md`。
