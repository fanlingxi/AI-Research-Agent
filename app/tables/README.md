# D040-F0 表格离线试点

本包不接数据库、Worker、快照或生产证据。只在显式命令中读取本地 PDF；导入模块、
`select-dev`、`packets`、`score`、`perturb` 不调用模型。Paddle 另需 `--enable-model`。
本文中的 PDF 提取、渲染及模型命令交给集成者执行；实现阶段只执行合成测试和 Ruff。

## 文件与输出

- `chars.py`：pdfminer 字符/线框、原文偏移对齐；`detect.py`：题注、线框、数字列簇候选。
- `engines.py` / `paddle_worker.py`：已有 MinerU HTML、独立 Paddle 环境结构适配。
- `verify.py` / `perturb.py`：填格、各项核验、五类扰动、由网格派生 HTML。
- `pilot.py` / `render_worker.py` / `resources.py`：离线流水线、可选本地渲染、资源采样。
- `packets.py` / `score.py`：盲化 X/Y 评审包、严格逐格 JSON 校验、分语言及合并计分。

每个论文运行目录必须为空，含 `run.json`、`pages/`（原文/字符 JSON 与页图）、
`crops/`、`raw/`（Paddle 请求与响应）、`tables/`（每表每引擎 JSON）、`report.html`。
页码从 1 开始，行列从 0 开始，跨页表保留独立 fragment，不自动拼接。
输出默认根目录为 `D:/AI-Research-Agent/data/evaluation/table-pilot`，批量运行应为每篇指定子目录。

## 集成者执行顺序（PowerShell）

现有主环境需 pdfminer.six 20260107、pypdf、Pillow；psutil 用于更完整的资源采样，
无 psutil 时 RSS 使用 Windows CIM，GPU 指标可能未知。Paddle 及其缓存只在独立环境中。
不安装或下载任何模型；缓存缺失时子进程失败并留作未核验。

```powershell
Set-Location D:\ARA-wt-tables-f0
$py = 'D:\AI-Research-Agent\.venv\Scripts\python.exe'
$dev = 'D:\AI-Research-Agent\data\evaluation\parser-trial\dev'
$trial = 'D:\AI-Research-Agent\data\evaluation\table-pilot\dev-f0'
$paddle = 'D:\AI-Research-Agent\data\tools\parsers\paddleocr-vl\.venv\Scripts\python.exe'
$env:PYTHONDONTWRITEBYTECODE = '1'

# 1. 只读已有 JSON，选两家既有输出或已选图像包中含表的开发集页面。
& $py -m app.tables.pilot select-dev --dev-root $dev --output "$trial\selection.json"
$selected = Get-Content -Raw -Encoding UTF8 "$trial\selection.json" | ConvertFrom-Json

# 2. 集成者执行：每篇所选页，模型调用仅 PP-Structure；MinerU 只读既有输出目录。
foreach ($paper in $selected.papers) {
    $runRoot = Join-Path $trial $paper.paper
    & $py -m app.tables.pilot run --pdf $paper.pdf --paper $paper.paper `
        --pages ($paper.pages -join ',') --language $paper.language --root $runRoot `
        --page-png-dir $paper.page_png_dir --mineru-json $paper.mineru_json `
        --engines mineru ppstructure --enable-model --paddle-python $paddle
    if ($LASTEXITCODE -ne 0) { throw "运行失败：$($paper.paper)" }
}

# 3. 查看各目录 report.html；为已经核验的表执行扰动审计。
# 零已核验表时命令退出非零，不能把空测试当作通过。
foreach ($paper in $selected.papers) {
    & $py -m app.tables.pilot perturb --root (Join-Path $trial $paper.paper)
}

# 4. 生成逐页盲包：包含每个检出表裁剪图、两引擎派生表格，以及整页供漏检检查。
foreach ($paper in $selected.papers) {
    & $py -m app.tables.pilot packets --root (Join-Path $trial $paper.paper) --seed 40
}

# 5. 仓库外独立标注/仲裁。只给标注者 packets/；不要给 private/、raw/、tables/。
# 每篇分别保存 judge-<name>/<packet_id>.json；严格照 response_format 填原图每一格。
# 两位裁判分别放 judge-doubao、judge-qwen；仲裁后的完整结果放 judge-consensus。
# scorer 不会自行调用裁判或把两人输出自动称为共识。

# 6. 合并全部论文，含漏检分母，分中文/英文输出 Wilson 95% 区间。
$runRoots = @($selected.papers | ForEach-Object { Join-Path $trial $_.paper })
& $py -m app.tables.pilot score --roots $runRoots --judge consensus `
    --provenance 'AI 共识 + 集成者看图仲裁，非人工金标；开发集' `
    --output "$trial\score-consensus.json"
```

标注调用由集成者在仓库外执行并维护 D040 的 ≤90 次预算；本包没有网关/API 调用代码。
选择来自已有解析器的信号，会有选择偏差，不能作为全体页面的无偏召回估计。
`run --pages 1-12` 可纳入无信号页面；即使某页零候选，评审包也保留整页并要求列漏表。
保留集不得使用这些开发集结果充当未见结果。

单表 Paddle 子进程可用主流程生成的请求重放（**会执行模型**）：

```powershell
$env:PADDLE_PDX_CACHE_HOME = 'D:\AI-Research-Agent\data\tools\parsers\paddleocr-vl\home'
$one = Join-Path $trial '2025.ccl-1.12'
& $paddle app/tables/paddle_worker.py `
    --request "$one\raw\p008-t001.request.json" `
    --output "$one\raw\p008-t001-replay.json"
```

请求格式：`{"page_png":"绝对路径","bbox_pt":[x0,y0,x1,y1],"page_size_pt":[宽,高]}`。
子进程只依赖 Paddle 环境（Pillow/numpy/paddleocr），裁剪后调用
`TableRecognitionPipelineV2.predict(..., use_layout_detection=False)`。
脚本禁用模型源探测与 Python socket 连接，缺缓存时报错；这不是操作系统网络隔离的替代品。

完整性能试验应使用新目录，以 `--render-python <含 pypdfium2/Pillow 的 python.exe>`
替换 `--page-png-dir`。例如现有 MinerU venv 的 Python（以本机实际路径为准）。
这会把本次本地渲染计入端到端耗时；使用缓存 PNG 的运行明确标记渲染历史耗时未知，
不能据此声称通过 D040 速度门槛。每篇论文使用一个持久 Paddle JSON-lines 子进程；模型仅加载一次，冷启动在该论文的表间均摊。
MinerU 结构时间只代表读取既有 HTML 后的对齐，**不含此前 MinerU 推理成本**，
不能直接据此比较两种引擎的完整生产成本。

## 坐标与核验假设

1. 坐标为应用 `/Rotate` 后的 MediaBox 左上原点、PDF 点，向右/下为正。
   pdfminer 已应用页旋转并移平 MediaBox；代码将底部原点转换为顶部原点，并应用 `/UserUnit`。
   页 PNG 必须对应同一完整页和旋转；CropBox 不同于 MediaBox 或纵横比不符时整页候选未核验。
   比例采用实际 PNG 宽高/页点宽高分别换算，不假定恰好 150 dpi。
   裁剪向外取整，返回格框使用**实际整数裁剪原点**，支持矩形和四边形外包框。
2. 原文偏移是该页未经 strip、乱码修复、伪粗体折叠的 `pypdf.extract_text()` Unicode 索引，
   不是 prepared 行文本偏移，不是字节或文档全局偏移。只忽略空白；中文逐字，不替换字符。
   正反向顺序匹配一致才接受部分对齐；重复串产生竞争偏移时保留不可映射。
   CCL 检测复用 `pdf_tools.has_garbled_glyph_decoding`；整页不可映射，不能使用修复文字冒充引用。
3. 核验分母包含候选区域相交的所有非空白字符，明确题注框和表注框内字符排除。
   字符必须完整落入唯一格框；跨边界/重叠算歧义。数字、Unicode 标点和符号需全部正确归位；
   全部引用必须能回读原字，重复偏移、旋转文字、多码点字形等不可靠情形保守拒绝。
4. 同一逻辑边界的格框差容忍 2 点，同时要求真实格框不重叠、边界严格单调、逻辑槽完整。
   跨度必须与下方列/同行边界一致。默认自动推断 1–3 行列头与 0/1 列行头，可用
   `--header-rows N --row-header-cols M` 指定；允许分组行标题，跨头值分区保持未核验。
   单元格逐行排序、换行拼接；模型文本只是交叉核对，不能成为输出原文。
5. MinerU 按 HTML 逻辑格顺序与区域字符连续精确匹配；不跳过或猜补 OCR。
   在已匹配字符之间的可观测空隙推断格边界。空格、重复布局/多行交织、无法建立边界等
   会降低已核验覆盖率，保留错误原因供审阅，不能当作已成功解析。
6. 确定性几何/引用检查不能证明任意语义表头解释正确，因此还必须逐格看图评审。
   五类扰动用于已知结构损坏回归；不声称可以检测所有视觉上也合法的任意重划分。
   F0 的 `evidence_eligible` 始终为 false；未核验格强制禁止证据消费。

## 指标与限制

`run.json` 记录完整墙钟、各引擎每表 p95、检测/渲染/结构/核验以及共享开销摊销，
失败结构调用也进入样本；无候选页的成本计入论文和每表摊销。
每论文耗时可与 5 分钟门槛对照，但所选页不足 12 页时不能宣称通过“12 页论文”门槛。
资源是主进程及子进程树 RSS 的采样最大值、进程显存、整卡显存，采样间隔 0.5 秒；
短峰值可能漏采。Windows/WDDM 不提供的进程显存和共享显存保持 null，不能写成 0 或通过。
Paddle 的分配器峰值另放 worker_resources，它不等同于整个进程显存。

评审 JSON 必须列完每个原图逻辑格（遗漏格判错），并对 X/Y 使用相同原图网格；
遗漏页表计入真实表总数。分母定义：已核验真表/全部真表（含漏检），已核验格中
内容与归属同时正确的格/原图格加无法归属的多余格，整表全对/所有已核验表。
误检但被核验的表另记并计入准确率惩罚，避免被静默丢弃。
95% 区间使用 Wilson；格级区间按独立格近似，仅描述性，不忽略同表相关性的局限。
缺标注、额外文件、重复键、重复格、空分母、不同引擎原图网格不一致都不会默认为通过。

## 断网验证

```powershell
New-Item -ItemType Directory -Force .pytest-tmp | Out-Null
$env:PYTHONDONTWRITEBYTECODE = '1'
$tests = @(Get-ChildItem tests/test_tables*.py | ForEach-Object FullName)
& $py -m pytest -p no:cacheprovider @tests -q --basetemp=.pytest-tmp/t
& $py -m ruff check --no-cache .
```

测试硬性阻断网络、子进程和真实 PDF 提取。图像仅为本地合成白底夹具；适配器调用用替身，
不会运行 Paddle、MinerU、真实 PDF 解析器或图像标注服务。


## Fix 1：纯离线 dev 复核

以下命令只读选定 dev 页的已有字符 JSON 与 MinerU JSON，不读取 selection 中的 PDF，
不启动任何模型或网络请求。路径中含 holdout 的输入会被拒绝。

```powershell
& D:/AI-Research-Agent/.venv/Scripts/python.exe -m app.tables.offline `
  --selection data/evaluation/table-pilot/dev-f0/selection.json `
  --saved data/evaluation/table-pilot/dev-f0c `
  --output .pytest-tmp/f0-fix1
```

`detect-report.json` 按页记录候选、caption、captioned、拒绝原因及 caption ground truth。
`mineru-offline-report.json` 记录逐表结果和失败原因；对应完整核验记录位于各论文 tables 目录。
检测按栏分段、合并无固定纵向间隔的 booktabs 规则；fragment 默认 false。
两个引擎共用字符间隙边界吸附，无法落入空隙的边界保持未核验；阈值未放宽。
表头默认按 midrule、HTML th/thead、首行的顺序推断，支持 1–3 行和跨列表头。
空的左上角及数值格允许存在，非空数值格仍须有非空列标题路径。


## Offline fix round 2

Replay only the saved development artifacts (no PDF extraction or engine invocation):

```powershell
& D:/AI-Research-Agent/.venv/Scripts/python.exe -c "from pathlib import Path; from app.tables.offline import rerun_saved; rerun_saved(Path('data/evaluation/table-pilot/dev-f0d'), Path('.pytest-tmp/f0-fix2'))"
```

The replay preserves detection candidates and MinerU saved topology, and emits both engines'
per-table verification plus `report.json`. Similarity uses casefolded, whitespace-free strings
with a 0.65 floor; empty/nonempty disagreement always fails. Every text difference is recorded.
The IoU floor is 0.5 and the engine-box centre must remain inside the snapped cell.
Only oblique, unmapped glyphs are excluded and recorded as watermark glyphs.
Source-mapped rotated glyphs still fail rotation verification. Ligature expansions require
exact consecutive source offsets. F0 outputs remain ineligible for production evidence.


## Fix round 3: offline source grid and engine topology votes

`python -m app.tables.fix3` re-extracts only the selected development PDF pages
with font metadata, then replays saved dev-f0d PP HTML and MinerU HTML (including
middle_json). Outputs stay in `.pytest-tmp/f0-fix3/`. `--reuse-pages` reuses that
enriched extraction; `--calibrate` compares the nine declared gutter settings.
No engine, model, or network call is made. The PDF allowlist permits only the
four named dev papers in the old holdout-v4 directory; holdout-d040 is forbidden.

`glyphgrid.py` derives line bands and body whitespace gutters independently of
engine geometry. Full rules separate rows; partial rules confirm header spans.
`vote.py` searches monotone row/column mappings, retaining the maximum sum of
cell-text similarities before requiring every cell to pass the 0.65 gate and
all glyphs to have one owner. Numeric body row merges fail the wrapping gate.
Two passing engines with different logical or physical partitions reject the
table. Accepted vote topology is retained so later perturbations cannot reuse
an earlier vote after changing its cells. The old engine paths remain diagnostic;
the reported union does not promote their tables into the new final status.

Font repair groups each font over the entire page, accepts only the explicit
Latin TeX family list and a uniform 0x5700/0x5B00 block, and preserves raw offsets
and `decoded_from`. Source-reference checks recompute the decoding rather than
trusting the annotation. Oblique-watermark exclusion is unchanged. Empty stub
headers are permitted for inferred row-label columns, including alphanumeric
labels such as BM25/T5; value columns still require a header path.

See the generated `report.json`, `calibration.json`, per-table JSON and
`summary.md` for counts, rejected mappings, remaining header cases and tests.


### Fix 5 offline replay and image judging

`python -m app.tables.fix5` reconstructs the four saved development paths and uses
`final.final_status`, also used by the pilot. Nonempty glyph ownership and header
paths must agree across every passing path. Empty-cell span differences are ignored.
The selected grid retains the accepted engine topology for subsequent corruption checks.
No model is called. Outputs are under `.pytest-tmp/f0-fix5/`.

`packets/manifest.json` contains blinded images and escaped HTML claims;
`packets/prompt.txt` defines the image judge task. Keep `private/blind-map.json`
away from judges. Supply one answer JSON per packet to each of two judge directories:

```
python -m app.tables.judge_score --manifest .pytest-tmp/f0-fix5/packets/manifest.json --judges judge-a judge-b --output .pytest-tmp/f0-fix5/judge-score.json
```

Scoring requires complete answers from both judges, unions erroneous coordinates,
and reports cell and whole-table proportions with 95% Wilson intervals. Out-of-claim
missing/extra cells add incorrect denominator cells. Results await arbitration.
