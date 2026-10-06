# Chat 整页图像工具（get_document_image）实施计划

## Context

Spec：`docs/superpowers/specs/2026-10-05-chat-page-image-tool-design.md`（e3de50d）。
私有 chat 只有 4 个文本工具，图形层内容（CONSORT 框图数字、KM at-risk 行）答不出
（benchmark #66/#84/#85）。本计划把 chat 工具集里的 `get_page_content` 彻底替换为
整页图像工具 `get_document_image(doc_name, page)`；只换 chat 车道，`/mcp`、
`document_context()`、`as_openai_tools()` 等消费面不动。

## 已验证的事实（fork @ e3de50d，v0.2.21）

- **原 PDF 的两层存储（同根）**：fork DocStore `docs/<doc_id>/` 只有三个 JSON
  （local_store.py:115-125，无 PDF、meta :158-168 无路径）；**服务层把原 PDF 持久
  存在 `<root>/<doc_id>/document.pdf`**（service api.py:68-73，索引后从 staging 拷入
  :503-504；另有按需生成的 `page_images/` JPEG 缓存，144 DPI，不保证全量，不依赖）。
  两层同根：helm `storageDir: /app/.pageindex`（values.yaml:39）≡ fork store 默认根
  （clients.py 不传 storage_path，client.py:553 默认 cwd 相对 `.pageindex`，容器
  cwd=/app）——本机 `.pageindex/` 磁盘验证 pi-*/document.pdf 与 docs/pi-*/ 并存；
  `document.pdf` 存储自服务第一版（086d3b1）就有，存量文档全覆盖。
- **TOOL_CONTRACT 与云契约逐字对齐**（agent_tools.py:66-68 docstring），冻结云契约
  `tests/data/cloud_mcp_contract.json` 只有 5 个工具、**无** get_document_image；
  `test_live_local_citation_prompts_match_cloud`（test_agent_tools.py:2407）钉 live
  一致性 → 不能往 TOOL_CONTRACT 塞第六个条目。
- **citation 冻结副本有守卫**：`LOCAL_CITATION_PROMPTS`（agent_tools.py:1634）三个
  格式各有一句 "Call get_page_content()"（:1637/:1647/footnote 同款）；
  test_agent_tools.py:2400 断言文本不含 get_document_image 且只点名
  `tool_names(include_management=True)` 里的工具 → 措辞替换必须走条件路径，
  冻结副本本身不动。
- **两条车道共用 `_tool_specs`**（agent_tools.py:1522）：OpenAI 车道
  `build_openai_tools`（integrations/openai_agents.py:77）→ `_ToolServer.call_tool`
  把 invoke 的 blocks 塞 `CallToolResult`（:62-66，模块注释 "images as images"）；
  Anthropic 车道 `build_anthropic_tools`（integrations/anthropic_sdk.py:27）同样消费
  `_tool_specs` 的 invoke，块经 SDK `mcp_content` 转换（:60-65），image block 原生
  支持。**唯一缺口**是本地 `local_invoke`（agent_tools.py:1547-1552）只吐 text 块。
- **chat 指令装配点**：OpenAI 车道 `_managed_instructions`（local_chat.py:28-33，
  `_openai_agent` tools 在 :431）；Anthropic 车道 `_anthropic_system`
  （local_chat.py:1302-1308，tools 在 :1436）。citation prompt 只在
  client.py:1236-1247 一处 prepend（`if self._local_chat: text =
  self.citation_prompt()`）。`_base_instructions`（agent_tools.py:1689）其余消费面：
  client.py:1907/:2038/:2109/:2137（agent_config / own-model 车道 /
  base_instructions property）——这些继续用默认文本工具集。
- **渲染设施**：`render_page_jpeg`（vision.py:206）JPEG data URL，RENDER_SCALE=1.5
  （:100）；scale=1.0 ≙ 72 DPI → DPI 换算 `scale = dpi / 72`。env 运行时读取模式见
  vision.py:112-127。
- 工具注册表：`_READ_TOOLS`（agent_tools.py:305）、`_IMPLEMENTATIONS`（:1180）、
  `call_tool` 按名分发且永不向 agent 循环抛异常（:1204-1260）；scope 经
  `_allowed_ids` 注入（:1237-1239）；`_resolve_document`（:396）做出 scope + 状态
  检查；`_failure` envelope（:319）。本地描述/参数描述覆盖：
  `_LOCAL_DESCRIPTIONS`（:1288-1301）、`_LOCAL_PARAM_DESCRIPTIONS`（:1303）、
  `_local_description`/`_local_schema`（:1316/:1320）。
- 现有测试锚点：默认本地工具集不含 get_document_image（test_agent_tools.py:133/
  1335）；云 bridge 的 get_document_image 假件（:680/:1589/:1644，用的是
  image_path 参数的云侧 schema，与本地实现无关、不动）。

## 设计要点（两处对 spec 的必要修正）

1. **源 PDF 解析 = 按服务布局写死推导，零拷贝零回填零 meta 改动**：原 PDF 自
   服务第一版（086d3b1，2026-08-28）起就持久存于
   `<root>/<doc_id>/document.pdf`，与 fork store 同根。
   `LocalAPI.get_source_path(doc_id)` 直接推导该路径，`isfile` 守卫只为把缺失
   变成 INVALID_INPUT envelope（"original PDF not available"）而非裸渲染异常。
   不加 meta sourcePath（部署侧是 staging 死键；纯本地模式经无源 → 明确
   envelope，接受）。**同根今天是部署巧合**（helm storageDir /app/.pageindex
   ≡ fork 默认 cwd 相对 .pageindex + 容器 cwd=/app，fork 不读
   PAGEINDEX_STORAGE_DIR）→ 可选加固（默认包含，review 可否决）：service
   clients.py 构造 client 时补一行 `storage_path=STORAGE_DIR`，随 SHA bump 一并
   部署，把同根变成构造保证，今天行为零变化。
2. **提示词措辞条件替换，冻结副本不动**：`LOCAL_CITATION_PROMPTS` 与
   `_READING_WORKFLOW` 的公共文本保持现状（:2400/:2407 两个守卫测试不破）；
   chat 车道装配时做替换——citation 文本经 `_page_image_prompt(text)`（一处
   `.replace("get_page_content()", "get_document_image()")`），
   READING_WORKFLOW 用 page_images 变体（get_document_structure() 定位 →
   get_document_image() 逐页看）。
3. 图像工具 schema/description 放本地常量 `_PAGE_IMAGE_TOOL`，不进 TOOL_CONTRACT
   （守住"契约逐字对齐云"的不变量与 live 测试）。

## 改动清单

### fork：`pageindex/local_api.py`

- 新增 `get_source_path(self, doc_id) -> str | None`：
  `Path(存储根) / doc_id / "document.pdf"`，isfile 才返回（服务布局约定写
  注释；LocalAPI `__init__` 记下 storage_path 根）。
- submit / meta / `local_store.py` 零改动。

### service（可选加固，默认包含）：`app/clients.py`

- `PageIndexClient(..., storage_path=STORAGE_DIR)` 一行——fork store 根与
  服务存储根由构造统一（今天两者恰好同根，行为无变化；防未来
  PAGEINDEX_STORAGE_DIR 与 cwd 默认分叉导致推导全落空）。

### fork：`pageindex/vision.py`

- `render_page_png(doc: Any, index: int, dpi: float) -> str`：`doc[index].render(
  scale=dpi/72).to_pil().convert("RGB")` → PNG bytes → base64 ascii str（沿
  render_page_jpeg 形状，不带 data: 前缀——MCP image block 要裸 base64）。

### fork：`pageindex/agent_tools.py`（主体）

- 常量：`PAGE_IMAGE_DPI_DEFAULT = 150`；`_page_image_dpi() -> float`（env
  `PAGEINDEX_PAGE_IMAGE_DPI`，调用时读取，沿 vision.py:112 模式）；
  `_PAGE_IMAGE_TOOL = {"description": ..., "schema": ...}`（参数 `doc_name` +
  `page`（1 起始整数，required））；`_READ_TOOLS_PAGE_IMAGES =
  ("browse_documents", "get_document", "get_document_structure",
  "get_document_image")`。
- `_get_document_image(client, doc_name, page, _allowed_ids=None) ->
  tuple[dict, bool]`（进 `_IMPLEMENTATIONS`，call_tool 文本面）：
  `_resolve_document` → 状态检查（沿 :1019-1024 的 not-ready 形状）→ page
  整数、1..pageNum 校验（越界 INVALID_INPUT，next_steps 给可用页数）→
  `client.get_source_path(entry["id"])`（缺失 → INVALID_INPUT "original PDF not
  stored; re-upload"）→ 渲染异常 → INTERNAL_ERROR。成功 payload 只含元数据
  （doc_name/page/dpi/total_pages）+ 说明 binary 内容仅 chat 车道投递。
- `_get_document_image_blocks(client, arguments: dict, doc_ids=None) ->
  tuple[list, bool]`：同校验路径，成功 →
  `[{"type":"image","data":<b64>,"mimeType":"image/png"},
    {"type":"text","text":json(元数据)}]`（图像在前，spec 顺序）；失败 →
  `[{"type":"text","text":_dumps(_failure(...))}]`，is_error=True。渲染实现：
  `pdfium.PdfDocument(path)` → `render_page_png(doc, page-1, _page_image_dpi())`。
  校验逻辑与 dict 版共享一个内部函数，避免两份。
- `_IMPLEMENTATIONS["get_document_image"] = _get_document_image`（call_tool 按
  名可调，但不在默认 `tool_names()` 里 → 其它消费面不可见）。
- `tool_names(include_management=False, page_images=False)`：True 时读集合换
  `_READ_TOOLS_PAGE_IMAGES`。
- `_local_description`/`_local_schema`：get_document_image 回退到
  `_PAGE_IMAGE_TOOL`（不触 TOOL_CONTRACT KeyError）；
  `_LOCAL_PARAM_DESCRIPTIONS` 加 `("get_document_image", "doc_name")`。
- `_tool_specs(..., page_images: bool = False)`：本地分支工具列表改
  `tool_names(include_management, page_images)`；invoke 构造处 get_document_image
  特判走 `_get_document_image_blocks`（其余仍走 local_invoke 文本包装）。云分支
  不动。
- 提示词：`_READING_WORKFLOW` 保持；新增 `_READING_WORKFLOW_PAGE_IMAGES` 变体
  （structure 定位 → get_document_image() 逐页；小文档直接看）；
  `_base_instructions(client, include_management=False, page_images=False)`
  选变体（AGENT_INSTRUCTIONS 拼装改为按 flag 选 READING_WORKFLOW 段）；
  新增 `_page_image_prompt(text: str) -> str`（citation 文本工具名替换）。

### fork：`pageindex/integrations/openai_agents.py`

- `build_openai_tools(..., page_images: bool = False)` 透传 `_tool_specs`。

### fork：`pageindex/integrations/anthropic_sdk.py`

- `build_anthropic_tools(..., page_images: bool = False)` 透传（:82-84）。

### fork：`pageindex/local_chat.py`

- `_managed_instructions`（:28）与 `_anthropic_system`（:1302）：
  `_base_instructions(client, page_images=True)`——chat 车道常开。
- `_openai_agent` 的 tools（:431）与 `run_messages` 的 tools（:1436）：
  `build_openai_tools(client, doc_ids=doc_ids, page_images=True)` /
  `build_anthropic_tools(client, doc_ids=scope, failures=failures,
  page_images=True)`。

### fork：`pageindex/client.py`

- citation prepend（:1236-1247）：`if self._local_chat:` 分支里
  `text = _page_image_prompt(self.citation_prompt())`（import 自 agent_tools）。
  仅此一处；own-model 车道与其余 `_base_instructions` 消费面默认 False 不变。

### fork：`tests/test_agent_tools.py` 追加/调整（沿现有 fake 手法）

1. `test_get_document_image_success_blocks`：真 PDF fixture（tests/data 现有
   小 PDF，或 pypdfium2 现场生成一页）→ 块类型与顺序（image 裸 base64 PNG +
   text 元数据 JSON）、is_error=False。
2. `test_get_document_image_invalid_input`：文档不在 scope / page 越界 /
   page 非整数 / 源 PDF 不可用（存储根下无 `<doc_id>/document.pdf`）→
   INVALID_INPUT envelope（文本块、is_error=True、next_steps 有可用页数或
   re-upload 指引）。
2b. `test_get_source_path_layout`：服务布局 `document.pdf` 在 → 返回路径；
   不在 → None（含 submit 零改动断言：meta 不新增 key）。
3. `test_page_image_dpi_env_read_at_call_time`：monkeypatch env，两次调用两次
   读取（沿 test_env_thresholds 模式，经 `_page_image_dpi()`）。
4. `test_tool_names_page_images`：`page_images=True` 集合含 get_document_image、
   不含 get_page_content；默认集合与 `tool_names(True)` 不变（:133/:151 锚点仍绿）。
5. `test_chat_lane_prompt_wording`：`_base_instructions(client,
   page_images=True)` 含 get_document_image() 不含 get_page_content()；默认反是；
   `_page_image_prompt` 替换后只点名 page_images 集合内工具。
6. `test_call_tool_image_text_envelope`：call_tool("get_document_image") 文本面
   返回元数据 envelope（b64 不出现在文本里）。
7. 守卫确认不动：test_citation_prompt_local_frozen_copy（:2389）、
   默认工具集断言（:133/:1335）保持原样通过。

### fork：`tests/test_local_chat.py` 追加

- 车道透传测试：fake agent 捕获 tools/instructions，断言 chat 组装用了
  page_images 工具集与变体指令（沿现有 chat 测试手法，不跑真模型）。

### fork：`pyproject.toml`

- `0.2.21 → 0.2.22`。

## 明确不改

- `TOOL_CONTRACT`、`tests/data/cloud_mcp_contract.json`、云 bridge 及其测试
  （:680/:1589/:1644）、`LOCAL_CITATION_PROMPTS` 冻结文本。
- `/mcp`（service `build_mcp_server` 默认 page_images=False）、
  `document_context()`、`as_openai_tools()`、`agent_tools()`、own-model 车道。
- 索引管线（vision pre-pass / garbled gate 51336da）、多页接口、图像缓存落盘、
  submit/meta（`local_store.py`、`local_api.py` 提交路径）、服务 api.py
  （服务端唯一可能的改动是可选的 clients.py 加固行）。

## 验证

1. 单测：`cd /Users/binhuchen/workspace/PageIndex &&
   /Users/binhuchen/miniconda3/envs/pageindex/bin/python -m pytest tests/ -q`
   全套（新测试 + 旧守卫全绿）。
2. **V1 smoke（SDK 图像通路）**：真 PDF（benchmark 语料 NEJM suppl 的 CONSORT 页）
   + `build_openai_tools(page_images=True)` + 本地 chat 模型，问
   "how many were excluded for inclusion/exclusion criteria" → 模型调用
   get_document_image 并读出 1638。若 Agents SDK 不转 MCP image block，回设计。
3. **V2 smoke（源路径）**：对部署侧已有 store 的 doc（`<root>/<doc_id>/
   document.pdf` 在）→ get_document_image 直接成功（零回填）；纯本地 submit
   一个 Downloads PDF（无服务布局目录）→ INVALID_INPUT envelope 而非裸异常。
4. **benchmark 重跑（必经验收）**：私有语料存量文档的 `document.pdf` 已在服务
   存储层，**无需重传/回填**，部署新 SHA 后直接跑
   `python tests/benchmark/benchmark_chat.py`，对比基线 86/91 正确、中位 14.4s；
   重点看延迟回退（图像 token）与数字密集题 VLM 误读（#82 类）。
5. 上线（征得同意后）：fork commit → push → 服务 requirements.txt SHA bump
   （+ clients.py 加固行，如采纳）→ 抽查（如 #66 CONSORT 数字）。

## 提交纪律

fork 改动先保持未提交供 review；确认后 commit（push 单独征得同意）；服务 SHA
bump 另行征得同意。
