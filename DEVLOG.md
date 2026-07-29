# DEVLOG

## 2026-07-30

### 修改內容（Changes）

- `main.py`：遷移至 MCP 規格 `2026-07-28` / `mcp` SDK 2.0.0
  - `@server.list_tools()` / `@server.call_tool()` 裝飾器 → `Server(on_list_tools=..., on_call_tool=...)` 建構子參數
  - handler 回傳值不再自動包裝，改為明確建立 `ListToolsResult` / `CallToolResult`
  - 刪除 `REQUEST_HEADERS` contextvar 與手動 ASGI wrapper，改用 `ctx.request.headers`
  - `StreamableHTTPSessionManager` + 手搭 Starlette → `server.streamable_http_app()`，healthcheck 走 `custom_starlette_routes`
  - `httpx` → `httpx2`；`types.Tool(inputSchema=)` → `input_schema=`（v2 全面 snake_case）
  - 加上 `cache_hints={"tools/list": CacheHint(...)}`（SEP-2549 要求的 `ttlMs`/`cacheScope`）
  - `server/discover` 由 SDK 自動實作，無需自寫
- `main.py`：安全與可用性修正
  - `response_headers` 改白名單（原本 `dict(response.headers)` 會把 Nextcloud 的 `Set-Cookie` session token 回傳給 MCP client）
  - `INTERNAL_HEADER_PARAMS` 加入 `authorization`，避免 OpenAPI header 參數覆蓋認證標頭
  - discovery 失敗改為 `DISCOVERY_RETRY_SECONDS` 後重試（原本永久卡住），加 `asyncio.Lock` 避免重試風暴
  - `nextcloud_discovery_status` 新增 `refresh` 參數可強制重新探索
  - CORS 由寫死 `["*"]` 改為 `CORS_ALLOW_ORIGINS` 環境變數，預設不開放
  - `GET /` 移除 `nextcloud_url` / `api_viewer_url` / `apps` / `last_error`（跨來源可讀，屬內網資訊洩漏）
  - stdio 模式改用環境變數憑證執行工具（原本要求 HTTP header，stdio 永遠拿不到，等於功能全壞）
- `Dockerfile`：`CMD` 由 `uvicorn main:app` 改為 `python main.py`，與 compose 統一（原本兩者不一致，直接跑映像檔會讓 `MCP_TRANSPORT`/`MCP_HOST`/`MCP_PORT` 全部失效）
- `docker-compose.yml`：移除多餘的 `command` 與已廢棄的 `version: "3"`
- `requirements.txt`：`mcp>=2,<3`、`httpx2`、`starlette>=1,<2`
- 新增 `test_main.py`（18 個測試）、`requirements-dev.txt`、`.gitignore`
- `README.md`：更新協議版本、新增環境變數、CORS 說明、stdio 認證行為、測試章節
- `.env`：使用者提供的本機驗證用檔案，未納入版控（`.gitignore` 已排除）。程式維持原本的環境變數形式，**不**讀取 `.env`；`docker-compose.yml` 保留原本寫死的 `environment:` 區塊。
- `README.md`：全文重寫。新增 Attribution 區塊註明來源為 `Rello/nextcloud-dynamic-mcp-server`（並載明上游無 LICENSE、授權條款屬上游作者），新增 "What This Fork Changes" 區塊逐項列出與上游 `bc2c042` 的差異，並補上「寫入類工具未經測試」的警語。

### 問題與解法（Problems & Solutions）

- **知識截止在 2026-05，不知道 MCP 2.0 內容**：沒有猜測，改為查官方 changelog 並在 scratchpad venv 實際安裝 `mcp==2.0.0` 逐一 introspect API 後才動手。
- **新舊協議分流機制不明**：一開始只在 body 的 `_meta` 帶 `protocolVersion`，伺服器仍回舊版格式。讀 SDK 原始碼發現 `streamable_http_manager._handle_request` 是看 **`MCP-Protocol-Version` HTTP header** 分流，補上後 `resultType`/`ttlMs`/`server/discover` 才正確。
- **啟動即探索遺失**：v2 的 Starlette lifespan 被 session manager 佔用，改用 `Server(lifespan=...)`，實測確認 HTTP 路徑會觸發。
- **整合測試只能跑一個**：`StreamableHTTPSessionManager.run()` 每個實例限用一次，改成 `build_mcp_app()` 工廠讓每個測試建新 app。
- **驗證 response header 洩漏是實際問題**：對真實實例發原始請求比對，Nextcloud 每次 Basic auth 回應都帶 `set-cookie: oc_sessionPassphrase=...`、`x-user-id`、`x-request-id`。修正前這些會進入每一次工具結果。過濾後 17 個 header 被擋，只留 content-type/content-length。

### 待辦事項（TODO）

- 本 repo 無 LICENSE。這是 fork，授權應由上游 `Rello/nextcloud-dynamic-mcp-server` 決定，不應自行補上 — 建議向上游反映。
- `NEXTCLOUD_URL` 仍是 module 層級常數，一台 server 只能綁一個 Nextcloud 實例。per-request header 只換帳號不換實例。若要支援多實例需改成 per-request URL + per-instance discovery 快取。
- 已對真實實例（nextcloud.tseng-network.com）做唯讀驗證：28 apps / 544 tools，代理呼叫回 200。**未測試任何寫入類工具**。
- 該實例的 `federation` app 在 `ocs_api_viewer` 回 500，discovery 會略過並記 warning（行為正確，但該 app 的工具不會出現）。
- 尚未推送至遠端，目前皆為本機未提交變更。
