# Chat 整页图像工具（get_document_image）设计

- 日期：2026-10-05
- 状态：设计已评审通过，待实施
- 仓库：PageIndex fork（`pageindex/`）；服务端零改动（部署时 bump SHA）

## 背景

cloud vs private benchmark（`pageindex-service` `tests/benchmark/`）暴露 3 例私有侧
失败，根因同源——**图形层内容不在文本索引里，而 chat agent 没有任何"看图"工具**：

- **#66**（MIRASOL CONSORT）：参与者流程图方框里的数字（1638）只存在于图形中，
  私有侧拒答；云侧靠 `get_document_image()` 读图答对（其回答里 "I need to read
  the figure image itself" 即调用痕迹）
- **#84 / #85**（ICON8 2019 附录 OS 曲线 at-risk 行）：私有侧定位到了正确的图但
  "无可读数字"，转而引用 2022 成熟 OS 文献的同类图——答案真实但来源错
  （417/199 vs gold 377/62）

私有侧现状：chat 工具集只有 4 个文本工具（`browse_documents` / `get_document` /
`get_document_structure` / `get_page_content`），本地模式刻意删除了 cloud-only 的
`get_document_image`（`agent_tools.py` 本地描述的 re.sub 与 `LOCAL_CITATION_PROMPTS`
的条目摘除）。

## 决策（已定）

1. **彻底替换**：chat 工具集里 `get_page_content` → `get_document_image`（整页
   图像），不是并存。数字读取此后全部依赖 VLM 识读——密集数字表格的误读风险已知
   （基线 86/91 正确里大量精确数字题），**benchmark 重跑是必经验收，不是可选项**。
2. **单页/次**：`get_document_image(doc_name, page)`，与云契约同名同义，token 可控。
3. **只换 chat 工具集**：`/mcp`、`document_context()`、`as_openai_tools()` 等其它
   消费面继续提供 `get_page_content` 文本，破坏面最小、可随时回退。

## 工具接口

```
get_document_image(doc_name, page)    # 单页，page 为 1 起始整数
```

- 成功：MCP content blocks =
  `[{"type":"image","data":<base64 PNG>,"mimeType":"image/png"},
    {"type":"text","text":<JSON: doc_name/page/dpi 元数据>}]`
  ——图像块供模型阅读，文本块供引用页码与溯源
- 失败：沿用 `_failure` envelope——文档不在 scope / 页码越界 → `INVALID_INPUT`
  （next_steps 给出可用页数）；渲染异常 → `INTERNAL_ERROR`；工具永不向 agent 循环
  抛异常
- 渲染：pypdfium2（`vision.py` 已有渲染设施），DPI 走 env
  `PAGEINDEX_PAGE_IMAGE_DPI`（默认 150，调用时读取），内存即弃、不落盘
- doc scope：同一 `_allowed_ids` 白名单机制（`call_tool` 注入）

## 工具集与提示词改动

**agent_tools.py**

- `_tool_specs(...)` 增加 `page_images: bool = False` 参数：True 时只读集合换为
  `browse_documents` / `get_document` / `get_document_structure` /
  `get_document_image`（`get_page_content` 不出现）。默认 False → 其它所有消费面
  （`tool_names()`、MCP server、`document_context()`）不变
- 新增 `_get_document_image` 本地实现（渲染 + envelope）；`TOOL_CONTRACT` 若无
  `get_document_image` 条目则从云契约冻结补齐 schema
- `READING_WORKFLOW`：大文档 `get_document_structure()` 定位 →
  `get_document_image()` 逐页阅读；小文档直接逐页看
- `LOCAL_CITATION_PROMPTS` 中 "Call get_page_content()" 措辞替换为
  `get_document_image()`；本地描述里摘除图像工具提及的 re.sub 反删除

**local_chat.py / integrations**

- chat 两条车道传入开关：`_openai_agent` → `build_openai_tools(..., page_images=True)`
  （`integrations/openai_agents.py`）；Anthropic 车道 `build_anthropic_tools` 同参

## 数据通路与实现期验证点

- 图像经进程内 MCP server 的 `CallToolResult` content blocks 回传
  （`openai_agents.py` 模块注释确认 "text as text, images as images"）；Anthropic
  车道原生支持 image block
- 实现期必须先 smoke 验证两点：
  - **V1**：OpenAI Agents SDK 确实把 MCP image block 转成模型可见图像输入（fake
    工具回图像块 + 多模态模型实测；若框架不支持需回设计）
  - **V2**：本地 store 持有原始 PDF 的可用路径（渲染需打开原文件；路径失效要报
    明确错误，不能裸异常）

## 测试

- 单测（沿 `tests/` 现有 fake 手法）：
  1. 成功路径块类型与顺序（image + text 元数据）
  2. 页码越界 / 文档不在 scope → INVALID_INPUT envelope
  3. DPI env 调用时读取（沿 `_min_text_tokens` 测试模式）
  4. `page_images=True` 工具集不含 `get_page_content`、含 `get_document_image`；
     默认集合不变
  5. READING_WORKFLOW 与 citation prompt 措辞已替换
- smoke：真 PDF（benchmark 语料里的 NEJM suppl）+ 多模态 chat 模型，确认 CONSORT
  图数字可读出

## 验收与上线

1. fork 单测全绿（`tests/ -q` 全套）
2. benchmark 重跑（`pageindex-service` `tests/benchmark/benchmark_chat.py`）对比
   基线：私有 86/91 正确、中位 14.4s。重点观察：延迟回退幅度（图像 token 远高于
   文本）、数字密集题的 VLM 误读（#82 类 148/146 精度场景）
3. 上线：fork push → 服务 `requirements.txt` SHA bump（单独征得同意）→ 重传/验证

## 非目标

- `/mcp`、`document_context()`、`as_openai_tools()` 工具面变化
- 索引期 vision 扩展（garbled gate 已合入 51336da；图形密集页转录另行评估）
- 多页/次接口、图像缓存落盘
