# 本地定制变更台账

2026-11-23 根据 Git 历史补录。范围为本次共同祖先之后的本地独有非合并提交；不声称覆盖仓库创建以来的全部变更。历史提交没有重新逐个检出验证，当前集成状态以最新同步测试为准。

以下定制在合并提交 `1c20e35` 中均保留（设计文档为保留归档）。表中的原因来自提交及实现，不替代原始需求记录。

| 日期 | 提交 | 变更 | 原因 | 主要位置 |
| --- | --- | --- | --- | --- |
| 2026-09-17 | 0990c21 | 同步聊天包装器复用共享事件循环 | 避免同步调用跨循环复用异步资源 | pageindex/local_chat.py |
| 2026-09-18 | 0b7a5d5 | 短文档跳过 verify；PDFium 线程锁；逐页提取回退 | 避免 1–2 页文档误拒绝，限制 PDFium 并发并容忍单页提取失败 | pageindex/page_index_classic.py、pageindex/_pdfium_lock.py、pageindex/local_api.py |
| 2026-09-28 | b6eed12 | 稀疏页视觉预处理 | 为扫描页及图像密集页补充构建目录所需内容 | pageindex/vision.py、pageindex/local_api.py |
| 2026-09-29 | 9a5b814 | temperature=0；None 页偏移保护 | 稳定目录抽取，防止无法匹配偏移时计算异常 | pageindex/utils.py、pageindex/page_index_classic.py |
| 2026-09-30 | 82c177a | 视觉预处理的 PDFium 子进程隔离 | 避免 PDFium 原生崩溃终止主进程 | pageindex/vision.py |
| 2026-09-30 | 7503b1a | mode='toc' 书签目录模式 | 直接使用 PDF bookmarks 构建树，无需 LLM 生成目录 | pageindex/local_api.py、pageindex/client.py |
| 2026-10-01 | f98cd1c | LLM completion 使用流式传输 | 应对代理读取超时 | pageindex/utils.py |
| 2026-10-01 | 51336da | 乱码页视觉转写 | 从页面图片恢复损坏文本层内容 | pageindex/vision.py |
| 2026-10-05 | e3de50d | 页面图片工具设计文档 | 记录图片读页设计 | docs/superpowers/specs/2026-10-05-chat-page-image-tool-design.md |
| 2026-10-06 | d7ec287 | 本地聊天通过渲染图片读页 | 支持模型阅读图表、版式和页面文本 | pageindex/agent_tools.py、pageindex/local_chat.py 及 integrations |
| 2026-10-06 | ee1d981 | 接受裸 physical_index 标记 | 兼容模型返回无尖括号的页标记 | pageindex/page_index_classic.py |
| 2026-10-06 | 762e480 | 页面图片 DPI 可配置，默认 110 | 控制页面图片渲染参数 | pageindex/agent_tools.py |
| 2026-10-06 | 8e729b0 | TOC 补页容忍 physical_index 缺失 | 避免缺字段导致索引失败 | pageindex/page_index_classic.py |
| 2026-10-07 | 376352f | 图片工具一次最多读取 5 页 | 允许相邻页面批量读图 | pageindex/agent_tools.py |

## 2026-11-23 集成适配

提交 `1c20e35` 同步官方 `add6b17`，主要适配如下：

- 流式 completion 使用官方重试规则，并从流式 chunk 累计用量；允许 backend 覆盖默认参数。
- 视觉标准索引及 toc 模式接入官方新增的 _with_backend 用量/模型参数。
- 图片工具参数与官方 collection、seen、annotations 参数并存；引用格式仍经过本地图片工具措辞转换。
- toc 输出测试改为官方统一的 start_index 字段；重试测试模拟官方识别的网络异常；verify 失败测试改为超过短文档阈值的输入。
- 公共测试 fixture 隔离视觉模型调用，专用视觉测试保留原有覆盖。

验证：隔离环境全量测试 1345 passed、7 skipped；详见[同步详情](2026-11-23-upstream-sync.md)。未来修改应在本表或后续日期条目明确状态为保留、调整或由官方替代。
