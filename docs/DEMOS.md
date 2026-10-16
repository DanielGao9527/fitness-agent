# 五个演示与复现

核对：2026-10-16。本轮只做既有能力的演示、录制和文档整理，新增供应商请求为0，业务源码与正式语料未改变。

本地演示包位于 `artifacts/final-demos-20261016/`，入口是 `index.html`，含五类场景、9段录像和实际结果。视频、截图与工作材料不随Git分发；仓库保留本说明与[匿名汇总](../evals/results/final-demos-2026-10-16.json)。本地查看器运行时可打开 `http://127.0.0.1:8773`；关闭后可在该目录运行 `python -m http.server 8773 --bind 127.0.0.1` 再查看。

| 场景 | 看到什么 | 本次证据性质 |
| --- | --- | --- |
| 1. 检索对照 | 同题旧双字、jieba＋向量RRF、向量缺失降级；撤回来源不再命中 | 当前检索器＋已有真实向量缓存；不是新模型调用或新质量分数 |
| 2. 记忆与实际记录 | 长期哑铃确认→新会话→完成上肢后改下肢→更正徒手→旧建议失效 | 隔离UI/API，模型替身；上一批2次真实理解另有[报告](../evals/results/memory-training-closeout-2026-10-16.json) |
| 3. 临时条件与补问 | 范围不明不保存，不确定先补问；今天、本次和长期条件分开 | 隔离UI/API；不将临时器械写成长期档案 |
| 4. 有界纠正 | 零命中恢复、两次无资料停止、相同证据停止、否定词改写被拒 | 24个冻结场景、固定响应和虚拟时钟；不是语义准确率或真实延迟 |
| 5. 受控知识更新 | 本人反馈→公共摘录→去重/检查→人工批准→切换→回滚；缺编码时失败保留旧包 | 独立合成标签/向量的测试服务；正式协议仍阻止未经质量验收的候选上线 |

9段录像已逐一检查解码、时长和实际播放；演示总览在1280、390、320像素无横向溢出，桌面和390像素截图已目视核对。

## 观看顺序与讲解要点

1. **检索对照**：固定取开发集按ID排序的前三题，未按检索结果挑题。打开三路Top 6，再查看撤回副本的结果。正式133块审核语料未变。历史T2五种方案一并展示，包含纯向量88.51%高于当前混合84.16%的结果。
2. **记忆与实际记录**：在聊天中核对长期器械；新会话只说“今天练什么”。通过“记录训练”表单核对，此时尚未保存；明确保存已完成上肢记录后，旧建议失效并改为下肢。计划记录不计完成。记录改回原样也不能复活旧建议；档案表单更正徒手后不再出现哑铃动作，刷新不自动生成或入账。
3. **临时条件与补问**：先展示范围不明和不确定表达，再确认长期哑铃、今天徒手及本次哑铃。今天/本次条件不覆盖长期档案。“未来条件”和跨日到期另由现有后端回归覆盖，不把录像说成真的等待了一天。
4. **有限纠正**：依次展开 `zero_recovery`、`both_empty`、`same_evidence`、`negation_rejected`。查看原问题、引用ID、停止原因及模拟调用数。最多两轮检索、一次改写，不是无限循环。已有真实开发集单/两轮均11/12、净0个百分点；回放恢复成功不能替换该成绩。
5. **受控知识更新**：按A反馈/私人候选、B公共摘录预审/权限、C构建/批准/切换/回滚三个章节观看。页面不允许普通用户自授审核权；私人问题和意见不进入公共版本。合成协议只用于工程演示，短摘录缺少匹配向量时停止构建，既有包保留。

## 在源码中复现

使用Python 3.12的项目虚拟环境。Node需能解析Playwright，并有本机Chrome；录像额外使用该Playwright版本配套的ffmpeg。这些属于测试工具，不是应用新增功能。

先清除测试进程的真实云凭据，给默认应用导入指定隔离路径，不向日常8765造数据：

~~~powershell
$env:DASHSCOPE_API_KEY = ''
$env:FITNESS_MODEL_PROVIDER = 'disabled'
$env:FITNESS_DB_PATH = Join-Path (Get-Location) 'artifacts/demo-import-only.sqlite3'
~~~

**场景2/3**：一个终端启动临时8772服务，另一个在同一源码目录顺序运行页面套件：

~~~powershell
python tests/ui_fixture_server.py --fact-versions
~~~

~~~powershell
node tests/browser-memory-training-chain.cjs
node tests/browser-equipment-scopes.cjs
~~~

**场景5**：另用8767合成服务。不要将这些标志用于正常应用，不要为正式账号授予测试角色：

~~~powershell
python tests/ui_fixture_server.py --coach --knowledge-build --knowledge-publish
~~~

~~~powershell
$env:FITNESS_TEST_PYTHON = (Get-Command python).Source
node tests/browser-knowledge-workbench.cjs
node tests/browser-knowledge-review.cjs
node tests/browser-knowledge-builds.cjs --publication
~~~

结束后关闭本次8772/8767测试服务，保留日常服务。测试只对各自固定的隔离端口/数据库有效，不把地址改成8765。

**场景4**：无需密钥、浏览器或私人数据库；输出目录必须是新的目录：

~~~powershell
python scripts/replay_knowledge_loop.py --output artifacts/demo-loop-new
~~~

**场景1**：本机包已经保留实际Top 6及来源撤回副本。要重跑已有检索评测，将 `artifacts/retrieval-eval/` 中已有 `document-vectors.sqlite3` 和匹配的 `public-queries-*.json` 复制到新的 `artifacts/demo-retrieval-new/`，然后执行：

~~~powershell
python scripts/evaluate_retrieval.py --model text-embedding-v4 --split dev --date 2026-10-16 --output artifacts/demo-retrieval-new
~~~

不添加 `--live`。缓存缺失或身份不匹配应停止，不能临时花费预算或把合成向量冒充真实语义结果。独立克隆不分发向量缓存；缓存准备方式见[脚本说明](../scripts/README.md)。来源撤回、缺缓存和模型不可用的隔离回归见[混合检索测试](../tests/test_hybrid_retrieval.py)。

## 交付后仍保留的边界

本轮五类演示和文档整理已完成，不能据此宣布所有模型质量、公网部署或实体手机权限验收完成。E2原始失败/确认下降、E3留出未执行及一般问题误拦截、正常知识发布的独立标签复核继续保留，见[STATUS](STATUS.md)和[ROADMAP](ROADMAP.md)。本轮不新增语料、自动提取、更多记忆层或周/月自治规划。
