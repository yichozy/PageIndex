# PageIndex 官方同步记录（2026-11-23）

## 比较范围

- 官方：https://github.com/VectifyAI/PageIndex ，main `add6b17cebff528612748ede3fed565b5f726c6b`（提交日期 2026-11-21）。
- 同步前本地：`376352f877841a7b1734d8d03b21e36f4de91650`。
- 共同祖先：`8fede179c6f8bf0df6d7b4fcab5ce44d5d455d6e`。
- 本地独有 15 个提交（含 1 个合并提交）；官方独有 147 个提交（含合并提交）。
- 相对共同祖先，官方涉及 95 个文件，24,195 行新增、4,501 行删除；本地涉及 23 个文件，3,164 行新增、118 行删除。行数包含测试、文档和 notebook，不等于产品代码量。

## 双方修复是否重复

`git cherry add6b17 376352f` 对 14 个本地非合并提交均报告 `+`：没有补丁完全等价的本地提交。功能方向有交集，不代表问题已被完整替代：

| 领域 | 官方更新 | 本地处理与合并决策 |
| --- | --- | --- |
| 页面图片 | 新增公共图片/裁剪 API、图片引用校验与引用解析 | 保留本地聊天以图片读页、一次最多 5 页、可配置 DPI（默认 110）；与官方集合范围、已读页面追踪参数并存 |
| LLM 稳定性 | 官方 #622 统一重试规则、保留推理上下文；#572 增加用量统计 | 保留本地流式调用以应对代理读超时，接入官方重试及流末用量统计；保留 temperature=0，允许 backend 覆盖 |
| 异步执行 | 官方扩展协议返回、流式生命周期与 Claude 工作流 | 保留本地同步包装器复用共享事件循环的修复 |
| PDF 提取 | 官方改进 Flash 字体、字符间距、顺序解析内存及页眉目录识别 | 保留本地逐页 PDFium 回退、线程锁及子进程崩溃隔离；保留稀疏/乱码页面视觉转写 |
| 目录构建 | 官方统一树结构、补充 intro 节点和 expand 修复 | 保留本地 mode='toc' 书签直建树；输出遵循官方 start_index/end_index 格式 |
| 标准模式 TOC 容错 | 官方仍使用严格 physical_index 标记校验及常规 verify | 保留裸 physical_index、缺字段、None 页偏移处理和 1–2 页短文档跳过 verify 策略 |

## 同步方式

在 `codex/sync-upstream-2026-11-23` 分支合并官方提交，保留双方完整历史，不重写原 main，不推送远端。解决 10 个文件的内容冲突，并适配自动合并未发现的调用签名变化。

本地标准模式新增的视觉转写会让官方测试中原本仅模拟树构建的 fixture 触发模型请求，因此公共测试 fixture 默认模拟视觉预处理；专门的视觉测试继续完整测试转写与 PDFium 子进程路径。

## 调用方需关注的官方变化

- 树输出统一为 start_index/end_index 等字段；旧代码读取 page_index 时需迁移。
- 云端处理模式只接受 None 或 flash；standard 和本地新增 toc 不能用于云端。
- 官方将 build_index 更名为 build_outline_index，并新增 PageSearchClient、PageExtractClient 和 collection API；依赖旧方法的业务代码需核查。
- 默认索引/聊天模型更新为 gpt-6-luna / gpt-6-sol；自定义供应商应显式传入模型和 backend 配置。
- 官方提高 OpenAI、Agents SDK、LiteLLM 等最低版本。已安装环境必须重新安装项目依赖；当前全局 Python 中 OpenAI 与 Agents SDK 不兼容，不能将其跳过的测试作为验证结论。
- 保留本地的 pillow 运行时依赖及 PDF 图片相关配置。发布版本号仍为仓库原有 0.2.22，本次不发布包。

## 验证

在 `/tmp/pageindex-sync-venv` 隔离环境安装 `-e '.[claude,anthropic]' pytest`，运行完整 pytest。真实云服务和真实模型端到端行为不属于本次离线回归验证范围。

最终结果：**1345 passed, 7 skipped**，耗时 81.80 秒；`git diff --check` 通过。7 项跳过不能计作通过。测试环境包含 OpenAI 3.57.0、openai-agents 0.25.0、LiteLLM 1.110.2、Anthropic 2.12.0 和 claude-agent-sdk 0.2.187。

## 官方更新摘要

以下是从共同祖先到 add6b17 的最终更新方向，不将期间中间依赖版本调整逐条作为当前行为：

| 方向 | 内容 | 官方提交/PR |
| --- | --- | --- |
| 树结构与导航 | 本地和云端统一文档树；补充 intro/expand 修复；新增 get_node、get_node_parent、get_node_path、get_node_map | 6d23caf / #541、d57adcd / #554、60c8ff8 / #542 |
| Flash 性能与准确性 | 优化索引；摘要从深层开始并与 expand 重叠；顺序解析一次保留一页；修复字体字符间距、目录运行链接误遮标题、非拉丁标题和不可解析回复重试 | #566、#432、#645、#553、#577、#588、#571 |
| 推理、协议与用量 | 各协议保留推理上下文；统一 SDK 重试规则；返回运行消息；Messages 流支持 get_final_result；本地索引/聊天报告用量，含失败路径 | #622、#612、#607、#572、#659 |
| Claude | claude-code/ 通过 Claude 订阅执行索引和聊天；改进登录错误、SDK 兼容及密钥传递 | #595、#603、#600、#594、#626 |
| 云端搜索与提取 | 新增搜索页面/文档、PDF JSON 提取和页面/区域图片；拆出 PageSearchClient、PageExtractClient；支持稍后建立 outline 索引；改善重复上传/失败任务恢复 | #677、#681、#686、#690、#691 |
| 集合与元数据 | 集合创建/列表/删除、集合上传、chat(collection_id=)、元数据 schema 管理、collection_context | 1300606、#625、#668、#692 |
| 引用、命名与列表 | 引用链接/区域高亮/路径解析；过滤模型未见图片的引用框；统一命名规则、修复列表递归及边界 | #525、#524、#632、#673、ed06ec6、2a73636 |
| 模型、依赖和资料 | 更新默认模型及依赖下限；扩充 README、本地使用指南、Claude/引用/元数据/费用对比 notebook；整理仓库布局与 CI | #568、#539、#693、#694、#696 |

## 实际合并流程与证据

1. 检查工作区：开始时没有未提交改动；确认 origin 指向本地 fork，记录本地 HEAD。
2. 使用 `git fetch https://github.com/VectifyAI/PageIndex.git main` 获取官方 main，固定 FETCH_HEAD 为 add6b17；此次没有新增名为 upstream 的 remote。
3. 用 merge-base、rev-list --left-right --count、log 和 diff 比较双方；用 git cherry 检查补丁等价性。没有等价补丁不等于没有功能重叠，因此另行检查相关实现。
4. 创建 codex/sync-upstream-2026-11-23，执行 `git merge --no-commit --no-ff add6b17`。采用 merge 保留双方历史；没有 rebase 或重置本地代码。
5. 解决 10 个冲突文件：agent_tools.py、client.py、cloud_api.py、integrations/anthropic_sdk.py、integrations/openai_agents.py、local_api.py、local_chat.py、utils.py、tests/test_client.py、tests/test_local_chat.py。核心取舍见前文重叠表；继续检查自动合并后的签名和字段变化。
6. 编译检查、diff --check，并在临时隔离环境安装项目及可选依赖后运行全量测试。初轮 1341 passed、7 skipped、4 failed；修正流式空响应处理及测试兼容后，最终 1345 passed、7 skipped。
7. 生成双父 merge 提交 `1c20e353ab78ad4bf64dc2a7ed3b047ccd3ef0c2`，确认 add6b17 是 HEAD 祖先、工作区干净；没有推送或发布。

复查历史不需要依赖 FETCH_HEAD：

```sh
git merge-base 376352f add6b17
git rev-list --left-right --count 376352f...add6b17
git cherry add6b17 376352f
git show --no-patch --format='%H %P' 1c20e35
git merge-base --is-ancestor add6b17 1c20e35
```

验证命令：隔离环境 Python 执行 `-m pytest -q --disable-warnings --maxfail=5`。测试记录的 186 个 warnings 未逐项消除；本次没有验证真实云服务、真实模型和下游业务项目。

后续维护入口：[本地定制台账](local-changes.md)、[官方同步历史](upstream-sync-history.md)。本节及两个台账于本次同步之后补录，未修改产品行为。
