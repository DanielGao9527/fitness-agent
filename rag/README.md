# 本地知识检索

这里检索经过整理和审核的知识摘要，为资料浏览、问答及建议提供出处。用户档案、饮食、训练和对话属于业务数据库，不加入知识索引。

| 文件 | 职责 |
| --- | --- |
| [catalog.py](catalog.py) | 定义来源和段落结构，校验出处、复核日期与可用状态 |
| [rag_service.py](rag_service.py) | 同步资料、构建 SQLite FTS5 索引并检索段落 |

资料入口是 [data/knowledge/sources.json](../data/knowledge/sources.json)。索引按内容指纹更新，使用中文相邻双字拆分与 FTS5／BM25 检索，不依赖向量数据库或在线嵌入服务。索引文件独立于用户数据库，可由资料重新生成。

检索只使用有效的审核来源；撤回、过期或不可用资料不能靠旧缓存继续提供依据。没有匹配结果时不联网搜索兜底，也不让模型补造出处。

资料检索本身不调用模型。问答由 [services/knowledge_answers.py](../services/knowledge_answers.py) 将检索段落交给可选文字模型，并核对回答引用。原始来源、使用范围与复用说明见 [知识来源](../docs/KNOWLEDGE_SOURCES.md)。
