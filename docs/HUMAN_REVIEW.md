# 快速人工核验

入口：实验对比 → 人工核验（`/experiments/review`）。选择运行后逐项阅读、判断并提交。
一个核验项对应一个「题号 × 运行臂」报告，因此题数与核验项数可能不同。

数字键 1–9 按页面编号切换必答/禁止结论，Y/N 设置整体判断，Enter 提交，→ 跳过。
输入框内不触发快捷键。各判断初始为空，全部选定才可提交；失败保留输入，成功自动进入
下一待标项，并在下方显示刚提交项的 AI 对照。跳过项可以继续核验，已标项可以重选覆盖。

## 数据目录与联调

默认根目录为仓库下 `data/evaluation`。启动后端前可设置：

```powershell
$env:ARA_EVALUATION_ROOT = 'D:\AI-Research-Agent\data\evaluation'
```

程序注入可用 `ExperimentReadService(root=Path(...))`，传给 `create_app(experiment_service=...)`。
人工提交只写对应运行的 `judge-human/<key>.json`，不修改 packet、AI 裁判或正式知识库。
开发测试全部使用临时目录。指向真实归档后，用户在界面的提交会保存到该真实归档。

## API

以下路径均以 `/api/experiments/review/runs` 开头：

| 方法及后缀 | 内容 |
| --- | --- |
| `GET /`（请求时无末尾斜线） | `runs`：数据集、运行名、题数、核验项数、人工已标数、AI 裁判目录数 |
| `GET /{run_ref}/items` | `items`、`total`、`human_count`；先按 AI 分歧、再按未标注排序 |
| `GET /{run_ref}/items/{key}` | `packet` 与 `human`；已有人工标签才返回 `ai_judges` |
| `PUT /{run_ref}/items/{key}/human` | 保存人工判断，返回 `key` 和已保存的 `human` |
| `GET /{run_ref}/agreement` | `human_count` 与各 AI 裁判的配对一致率 |

`run_ref` 是列表返回的不透明标识（根目录相对路径的无填充 base64url）；调用方应原样使用。
路径分量采用安全名称白名单，拒绝路径穿越、绝对路径、Windows 设备名/备用数据流和链接别名。
`key` 是 packet 文件名去掉 `.json`，同样严格校验。

PUT 内容：`covered: {结论id: boolean}`、`violated: boolean[]`、`outcome_ok: boolean`、
可选 `note: string` 与 `corrected_note: string`。`judge` 可省略，若提供必须为 `human`。
结论 ID 必须恰好匹配 packet，禁止结论数量必须一致，字符串/数字不能代替布尔值。
非法路径或结论不匹配返回 400，请求类型错误返回 422，不存在返回 404，归档损坏/读写失败返回 409。
重复提交原子覆盖，最后一次成功保存为准。

列表不会返回具体 AI 判断或理由。`ai_agree` 比较所有实际存在的 AI 裁判的三类判断，
忽略备注；不足两位时为 `null`。人工不参与此一致性状态。

一致率仅统计人工与该 AI 都已评分的项。整体按项计算；必答与禁止结论按单个布尔判断计算，
同时返回各必答结论 ID 的一致率。每项指标都有 `matched`、`total`、`rate`，分母为零时
`rate=null`。缺失 AI 记录不计入该裁判分母，孤立人工文件不计入已标数。
分歧优先抽样的结果不代表全量准确率；期望标签仍来自 AI 共识，纠正备注不会直接修改标签。

## 评分器

```powershell
python -m app.benchmarking.holdout_v2_score <run-dir> --summary --judges human
python -m app.benchmarking.holdout_v2_score <run-dir> --summary --judges human,api
```

也可调用 `summary(run_dir, judges=["human"])`。省略过滤时仍保守合并全部裁判，
包括新增的人工裁判；指定过滤后，不回退到其他裁判。尚未人工标注的项仍按原评分器的
未评分逻辑计入运行分母，因此只标注部分报告时不要将人工过滤的总分当作已标样本准确率。
`label_provenance` 继续描述期望标签的 AI 共识来源，`judging` 说明裁判过滤范围。
