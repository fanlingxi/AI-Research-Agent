# D039-B 开发集解析器试验

## D040-E：分区规则 v4 的离线 span 评分

```powershell
python -m app.benchmarking.parser_trial zoning-score --root D:\AI-Research-Agent\data\evaluation\parser-trial\dev --versions pypdf-v3 pypdf-v4 --out .pytest-tmp/zoning-dev
```

此入口只读取本地 PDF、`manifest.json`、`prepared/`、`consensus.json` 与
`arbitration.json`，不调用网络、模型或版面引擎，不改写试验数据。
输出目录必须在输入试验目录之外；默认写入 `.pytest-tmp/zoning-dev`。
产物为 `score.json`、中文 `score.md`、包含行号和原文的 `errors.json`。

真值先取共识，再由仲裁覆盖。逐行核对 PDF 原文偏移，以唯一完整覆盖该行的块
作为预测；缺失、部分覆盖、多重覆盖、原文变化或标签缺失均报错，不丢弃任何行。
唯一例外是 v4 按字体解码了 CCL 坏 TeX 字体的页（元数据 `tex_font_decoded_pages`）：
解码不增删换行，所以冻结的第 i 个非空行投影到 v4 该页的第 i 个非空行，要求行数相同、
且每行去掉 U+5700–577F/U+5B00–5B7F 后的汉字序列一致，否则仍按原文变化报错。
分别按中文和英文汇总五项指标：首个物理页前置信息 F1；页眉/页脚/页码被判为
排除类的召回；参考文献被判为排除类的召回；真值正文被判为任何非正文类别的比例；
仅在真值正文行上计算的段落起点 F1。正文类别包括 paragraph、abstract、heading、keywords。
排除类别为 front_matter、header、footer、page_number、references。

D040 门槛为 95%、95%、98%、≤0.5%、85%，同时每项在不利方向上相对 v3 不差
超过 1 个百分点；误排除率越低越好，其余越高越好。零分母为 null，不判达标。
即使仅请求 v4，也会计算 v3 以检查退步。

**开发集已用于调参；标签来自 AI 共识和仲裁，非人工，不能代表未见保留集。**
此评分入口始终输出 `adopt=false`；以下原 D039-B 试验及冻结基线保持原样。
生产新导入/重新解析按本次实施要求使用 v4，已保存来源和冻结的重解析任务按其原版本执行。
v4 的 `paragraph_start` 区分物理分块与逻辑段落，页眉或图表后的续行不会因此变成新段；
v3 不输出该字段，旧块和切片的序列化字节保持不变。

## D039-B 原始试验

开发集，只用于选候选；AI 共识标签，非人工。

本模块不改变生产解析策略或主项目依赖。导入模块不会调用模型、解析器、网络。
实际解析与标注由集成者执行；实现阶段只运行假客户端、假子进程和离线夹具测试。

## 文件与流程

| 文件 | 用途 |
|---|---|
| `common.py` / `prepare.py` / `render_worker.py` | 固定 12 篇清单、SHA-256、前 12 个物理页、稳定行 id、150 dpi PNG、pypdf-v3 块 |
| `normalize.py` / `align.py` | 类型映射表、PDF 点坐标、逐行预测与 unaligned 计数 |
| `runners.py` / `resources.py` / `paddle_worker.py` | 本机 MinerU API、独立 Paddle 环境、原始输出、逐页耗时、资源峰值与累计预算 |
| `packets.py` / `judge.py` | 12 个文本包、最多 40 个图像包、盲化映射、项目配置的 DeepSeek 文本标注 |
| `score.py` | 双标注一致标签、分歧与仲裁、分语言指标及 D039-B 门槛 |
| `tests/test_parser_trial*.py` | 离线确定性验证；HTTP、渲染与解析子进程全部注入替身 |

输入开发集固定：8 篇指定 CCL 论文（不含 2025.ccl-1.15）与 attrscore、cove、csqe、e5-mistral。
`--zh-dir`、`--en-dir` 只允许替换目录，论文名称仍然固定。空白页保留物理页码。
`data/demos`、`data/evaluation` 输入不修改。默认输出目录为
`D:\AI-Research-Agent\data\evaluation\parser-trial\dev`；每个子命令都支持 `--root`。

## 集成者依次执行

以下是待执行命令，不代表已经运行真实解析或标注。

```powershell
$trialPython = 'D:\AI-Research-Agent\.venv\Scripts\python.exe'
$trialRoot = 'D:\AI-Research-Agent\data\evaluation\parser-trial\dev'
$env:PYTHONDONTWRITEBYTECODE = '1'

# prepare 包含 manifest；manifest 也可以单独执行。
& $trialPython -m app.benchmarking.parser_trial prepare --root $trialRoot

# 集成者先启动本机 MinerU 服务，设置正确 MINERU_HOME；禁止 --remote。
# MINERU_HOME=D:\AI-Research-Agent\data\tools\parsers\mineru\home
# 在独立环境使用 mineru server start，不由本模块起停服务。
$mineruServicePid = (Get-NetTCPConnection -LocalPort 16580 -State Listen |
    Select-Object -First 1).OwningProcess
& $trialPython -m app.benchmarking.parser_trial run-mineru --root $trialRoot `
    --service-pid $mineruServicePid --max-pages 150
```

MinerU 完成后，集成者停止其服务并确认 GPU 已释放，再执行 Paddle。
运行器的根目录 GPU 锁防止两条试验命令同时运行，但不会停止驻留的 MinerU 服务，
也无法阻止其他根目录或外部程序使用 GPU。发生 HTTP 超时，先检查并终止未完成的
服务任务，再运行任何解析器。不要用新输出目录绕过已消耗的预算。

```powershell
& $trialPython -m app.benchmarking.parser_trial run-paddle --root $trialRoot --max-pages 150
& $trialPython -m app.benchmarking.parser_trial normalize --root $trialRoot
& $trialPython -m app.benchmarking.parser_trial align --root $trialRoot
& $trialPython -m app.benchmarking.parser_trial packets --root $trialRoot --seed 39 --max-images 40

# 事先将项目 LLM 配置设置为 DeepSeek。调用上限跨重启累计，失败不重试。
& $trialPython -m app.benchmarking.parser_trial judge-deepseek --root $trialRoot --max-calls 12
```

集成者在仓库外完成另一份文本标注及两份图像标注，不把辅助网关代码放入仓库：

- `packets/lines/lines-<paper>.json` → `judge-qwen-text/lines-<paper>.json`。
- `packets/images/image-<paper>-pNNN.json` → `judge-doubao-image/` 与 `judge-qwen-image/`，文件名不变。
- 图像包的 `png` 是本地文件路径；集成者的外部标注工具必须实际附上该 PNG。
- 仅把对应的包和 PNG 给各标注者；不得传递 `private/blind-map.json`、另一位标注者的答案、对齐预测或成绩。
- 答案严格遵守包内 `response_format` 和说明；无对象的数组返回 `[]`，无额外字段。
- `packets/index.json` 是有效包清单。最大计划调用数为 `3 + 12×2 + 40×2 = 107`，
  包括已发生的 3 次能力测试；外部标注、失败、重试及额外仲裁模型调用合计不得超过 120。
  本地代码只执行 DeepSeek 的最多 12 次文本调用；仓库外调用由集成者统一记账。

```powershell
& $trialPython -m app.benchmarking.parser_trial score --root $trialRoot `
    --line-judges deepseek qwen-text --image-judges doubao-image qwen-image

# 查看 arbitration.md 和对应页面 PNG。
# 在 $trialRoot\arbitration.json 写入 resolutions，再重算。
& $trialPython -m app.benchmarking.parser_trial score --root $trialRoot `
    --line-judges deepseek qwen-text --image-judges doubao-image qwen-image `
    --arbitration "$trialRoot\arbitration.json"
```

仲裁文件示例：

```json
{
  "resolutions": {
    "lines-2025.ccl-1.12/line/2025.ccl-1.12:p001:l0001": {
      "id": "2025.ccl-1.12:p001:l0001",
      "label": "front_matter",
      "paragraph_start": false
    },
    "image-2025.ccl-1.12-p008/X/tables/X001": {
      "id": "X001", "total_cells": 20, "error_cells": 1,
      "examples": ["第 2 行第 3 列数值错误"]
    }
  }
}
```

完整编号必须从实际 `disagreements.json` / `arbitration.md` 复制。漏检项的仲裁值是整个
`missed_tables` 或 `missed_formulas` 数组。缺少标注者的条目也进入分歧，绝不把单标当共识。
表格计数一致、仅错误示例措辞不同视为标签一致；漏检描述不同则保守送仲裁。
仲裁验证 id、类型、数值、必填项；未经仲裁的结果可以输出部分指标，但不能判定达标。

## 坐标、类型与待校正项

统一页码从 1 起，bbox 为 `[x0,y0,x1,y1]`，单位 PDF point，**左上原点，向右/向下为正**。

- MinerU：真实 4.0.12 样例 `page_idx=6/7` 对应物理第 7/8 页，bbox 在 `[0,1]`。
  页眉 y 为 `0.047..0.062`、页脚靠近底部，支持左上原点判断。
  `extensions.docvortex_layout.pages[].width_pt/height_pt` 提供页尺寸；分别乘以宽高。
  表格嵌套的 captions/footnotes 提取为独立块；保留表格 HTML 与公式 LaTeX。
  `MINERU_TYPES` 是明确映射表，`equation`→formula、`paragraph_title`→heading、
  `page_footnote`→footnote。未知类型保持 other，不猜测为正文。
- Paddle：当前没有提供真实 `paddle-*.json`；适配按简报给出的 `res.parsing_res_list` 或
  顶层 `parsing_res_list`，以 `block_label/block_bbox/block_content` 读取。
  worker 对 **150 dpi 单页 PNG** 调用 `PaddleOCRVL(pipeline_version="v1.6")`，
  因而假设 bbox 是该 PNG 的左上像素坐标，按实际 PNG 宽高换算，而非盲乘 72/150。
  真实运行前需确认 `.json` 包装层、标签、bbox 是否基于未变换的输入图、模型版本参数。
  如果实际框来自内部旋转/缩放图，必须先补逆变换，不能直接采用当前比例。
  `PADDLE_TYPES` 是独立映射表；`doc_title`→title、`display_formula`→formula、
  `table_title/figure_title`→caption、`number`→page_number。inline_formula 不当作行间公式。
- pypdf：复用 `pdf_layout.collect_text_runs` 与 `_lines`，保存真实 x、基线 y 和字号；
  转换 y 为左上坐标。原模块没有字形 advance/整行 bbox，因此只用行锚点及字号高度带
  与块重叠判定，不编造整行宽度。坐标缺失时只做文本匹配；重复且无法区分的行记 unaligned。
  基线另外保留精确文本 span，避免重复行误匹配；跨页延续段不另设段落起点。
- 行匹配去除排版空白、Markdown 强调和全半角差异，优先包含匹配，近似匹配需同时有
  空间支持。不凭空间位置把完全不同的文本当作匹配。正文落不到块时计入 unaligned。
- `tests/fixtures/parser_trial/mineru_excerpt.json` 是真实样例少量类型块的原样片段，
  删除了 base64 图片；完整保留一张表以覆盖 HTML 与嵌套表题。原 `.trial-samples/` 不修改。
  Paddle 仅使用合成最小样例，不能声称已验证真实接口。

## 运行记录、预算和统计口径

- 渲染只在 MinerU venv 的独立脚本里导入 pypdfium2；主项目无新增依赖。
- MinerU 只允许字面 loopback HTTP 地址，禁用环境代理和重定向。使用 uploads→content→complete→
  parse/jobs→files/content；上传命中 SHA 去重时直接使用返回的 file.id。
  不使用已知不可用的 `/v1/file_parse`、`/v1/tasks`，也不调用远程 MinerU。
- MinerU 每篇按物理页逐页提交 job，每个原始结果、job 响应和逐页耗时分别留存。
  这是为了用真实单页样本计算 p95；不把“整篇耗时 / 页数”复制成逐页数据。
  单页执行可能与整篇一次执行的上下文行为不同，是本试验明确的执行配置。
  `normalize` 也接受集成者导入的整篇 `structured_content.json` / `middle_json.json`，
  但只有整篇均值的运行不能通过逐页耗时完整性检查。
- MinerU 的 `parse.duration_ms` 在本地 4.0.12 实现中包含解析与输出序列化，
  另留 HTTP wall time；Paddle 记录消耗完单页 `predict` 迭代器的时间，JSON 落盘在计时之外。
  两者是各自接口的处理时间，非纯 GPU kernel 时间。Paddle 整批只初始化一次模型。
- 每次命令首次推理单独标为 cold，不进入 warm p95；Paddle 模型初始化秒数另存，
  MinerU 服务启动由集成者计时，可传 `--cold-start-seconds`，不知道则 null。
  缓存命中不计热推理；未提供逐页时间或计时页不完整不能通过门槛。
- 峰值内存是服务 PID/worker PID 与子进程 RSS 之和；优先 psutil，缺包时用 CIM/WMI。
  显存为 nvidia-smi **整卡已用显存，包含桌面**，因此是保守上界；不是伪造的进程显存。
  Paddle `--cpu` 明确选择 CPU，显存记 0。采样间隔默认 0.5 秒，缺失采样为 null，
  工具失败记录在 resource_errors。Paddle 整批资源峰值作为各论文的共同保守上界。
- `attempts/<parser>.json` 在推理前预占页数，累计最多 150；失败或中断也计入。
  已尝试论文不自动重跑。`judge-deepseek` 同理，`max_calls` 是累计上限而非每次命令新增额度。
  崩溃遗留锁需人工确认进程已退出后清除；不要删除账本重试。补跑须由集成者在总预算内
  明确安排并保留原账本，本实现不提供隐式重试入口。
- 标注输出失败时保存原始响应与失败类型；记录用量，未知费用/未知用量为 null。
  标注开始后拒绝重建包；推理开始后拒绝覆盖 prepare/manifest。
- 图像页取两家检出的 table/formula 页并集，表格优先，再按 seed 与 paper/page 的 SHA 排序，
  最多 40。两家都漏掉且无其他表/公式触发入选的页不在这个图像样本中；这个抽样局限保留在报告说明。
- 表格准确率按逻辑单元格微平均；漏表全格计错，误检表记 1 个错误格；合并格算一格。
  公式等价率含漏公式（不等价）。0 张表/0 个公式的指标为 null，不能当作 100%。
- 首页前置信息 F1 只看物理第 1 页；页眉/页脚/页码与 references 分开计算排除召回。
  排除指预测为 front_matter/header/footer/page_number/references；unaligned 不算成功排除，
  但对于真正文保守计入误排除率。段落边界 F1 以全部标注行作二元边界判断，非正文误报也计 FP。
- p95 使用 nearest-rank（排序后 ceil(0.95N)）；内存门槛单位按 GiB（1024³）实现。
  pypdf-v3 只评适用的行质量门槛，不把没有的表格、公式或运行性能算作零。
  两家中英分别全部达标后，用中英等权平均表格准确率选候选，差 <1pp 取平均 p95 更快者。
  本模块始终输出 `adopt=false`、默认 pypdf-v3；采用决定仍需新保留集。

## 离线验证

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
$trialTests = (Get-ChildItem tests/test_parser_trial*.py).FullName
& 'D:\AI-Research-Agent\.venv\Scripts\python.exe' -m pytest -p no:cacheprovider `
    $trialTests tests/test_pdf_layout.py -q --basetemp=.pytest-tmp
& 'D:\AI-Research-Agent\.venv\Scripts\python.exe' -m ruff check --no-cache .
```

PowerShell 不会替原生程序展开 `tests/test_parser_trial*.py`，因此命令先枚举测试文件。
不运行任何真实解析器、模型或网络 smoke test。
## D040 保留集

以下命令由集成者执行。`$paperList` 指向预先登记的 JSON 清单，格式如下；
每篇论文使用绝对 PDF 路径和预登记 SHA-256，`paper` 必须唯一，语言只能为 `zh`/`en`。
准备前检查全部摘要，manifest 保留 split、论文顺序与摘要；每篇最多取前 12 个物理页，
沿用稳定行号、150 dpi PNG 和 pypdf-v3 blocks。省略 `--papers` 时仍使用原开发集。

```json
{"split":"holdout-d040","papers":[{"paper":"registered-id","language":"zh","pdf":"D:/absolute/registered.pdf","sha256":"<预登记的64位SHA-256>"}]}
```

```powershell
$python = 'D:\AI-Research-Agent\.venv\Scripts\python.exe'
$trialRoot = 'D:\AI-Research-Agent\data\evaluation\parser-trial\holdout-d040'
$paperList = 'D:\AI-Research-Agent\data\evaluation\holdout-d040\papers.json'
$zoningOut = 'D:\AI-Research-Agent\data\evaluation\parser-trial\holdout-d040-zoning'
$mineruServicePid = 12345 # 替换为已启动的本机 MinerU 服务 PID

& $python -m app.benchmarking.parser_trial prepare --root $trialRoot --papers $paperList
& $python -m app.benchmarking.parser_trial run-mineru --root $trialRoot --service-pid $mineruServicePid --max-pages 150
& $python -m app.benchmarking.parser_trial packets --root $trialRoot --lines-only
& $python -m app.benchmarking.parser_trial judge-deepseek --root $trialRoot --max-calls 8

# 外部 qwen-text：独立标注 $trialRoot\packets\lines\lines-*.json。
# 按包内 compact spans/paragraph_starts 格式作答，然后用下方转换命令导入。
# 将原始回答保存在 $trialRoot\external-qwen-text\lines-<paper>.json。
@'
import sys
from pathlib import Path
from app.benchmarking.parser_trial.common import read, write
from app.benchmarking.parser_trial.packets import expand_line_answer, validate_answer
root = Path(sys.argv[1])
for packet_id in read(root / "packets/index.json")["line_packets"]:
    packet = read(root / "packets/lines" / f"{packet_id}.json")
    raw = read(root / "external-qwen-text" / f"{packet_id}.json")
    answer = validate_answer(packet, expand_line_answer(packet, raw))
    write(root / "judge-qwen-text" / f"{packet_id}.json", answer)
'@ | & $python - $trialRoot

& $python -m app.benchmarking.parser_trial score --root $trialRoot --line-judges deepseek qwen-text

# 仲裁：查看 disagreements.json / arbitration.md，核对对应原页 PNG。
# 人工写入 $trialRoot\arbitration.json，例如：
# {"resolutions":{"lines-<paper>/line/<完整行id>":{"id":"<完整行id>","label":"body","paragraph_start":true}}}
# 完成全部分歧后重新 score，再运行 zoning-score。
& $python -m app.benchmarking.parser_trial score --root $trialRoot --line-judges deepseek qwen-text --arbitration "$trialRoot\arbitration.json"
& $python -m app.benchmarking.parser_trial zoning-score --root $trialRoot --out $zoningOut --versions pypdf-v3 pypdf-v4
```

清单路径与服务 PID 按实际登记文件及运行进程替换。MinerU 仍使用本机 uploads +
`/v1/parse/jobs`，逐页保存 `structured_content.json` 和 `middle_json.json`，
沿用 150 页累计预算、锁和不自动重试规则；这些原始结果供单独的表格试验使用。
纯行包不依赖 normalize/align、Paddle 或图片裁判；DeepSeek 按本 root 的 manifest
校验包列表，`--max-calls` 为累计上限，硬上限仍为 12。

纯行 score 只生成共识、分歧和仲裁报告，不要求解析器预测或性能数据。
zoning-score 使用本 root 的完整共识及 arbitration.json，缺失标签则拒绝评分；
不读取开发集标签。输出目录必须在 trial root 外，报告分别列出中英文门槛、
相对 v3 不利方向不超过 1pp 的检查和通过/失败状态。
95% Wilson 区间按行计算；F1 使用 Jaccard 的 Wilson 区间经 `2J/(1+J)` 转换，
不将一个 TP 视作两个独立样本。区间不校正论文内相关性，门槛使用点估计，
零分母为 null，不能算通过；最终采用决定仍由集成者作出。
