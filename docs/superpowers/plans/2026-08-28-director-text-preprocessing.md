# 导演模式文本预处理层 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** 在导演模式导入与角色分析之间建立可审阅、可编辑、可恢复的文本预处理阶段，消除跨作者的引号、换行和纯标点分块问题，并可选择以 LLM 做等义配音改写。

**Architecture:** 原文继续只读保存；本地 StructuralTextCleaner 先对整篇文章完成跨段落引号扫描和确定性清洗，生成稳定段落、清洗稿与分析稿。可选 LLM 改写以段落内结构单元为单位，通过精确 ID 覆盖协议返回；失败段落回退本地稿。用户确认后，现有分析只消费已确认的 preprocessed_text，并沿用角色、翻译与生成流程。

**Tech Stack:** Python 3.11、Pydantic v2、SQLAlchemy async、Alembic/SQLite、FastAPI、原生 ES modules、pytest/httpx、Node.js。

## Global Constraints

- source_text 永远保留用户导入原文；不迁移或转换旧导演项目。
- 本机已获授权的旧项目只通过现有删除接口移出活动列表；公共升级不得自动删除用户数据。
- 默认模式为 structural；rewrite 仅做不增、不减、不改变剧情信息的等义改写；skip 仅作兼容/调试。
- 预处理保持原有语言，不做目标语言翻译，不改数字、ASCII/外语词、专有名词或引号结构。
- preprocessed_text 只有经用户确认后才能成为剧本分析输入；后续语句偏移量属于该文本。
- 配音候选必须含至少一个 Unicode 字母或数字；纯空白、纯标点、纯引号和 pause_marker 不得进入翻译或 TTS。
- 同一项目的预处理、分析、翻译后台命令采用单飞；重复点击不得创建额外 LLM 请求。
- 同轮分析或翻译子请求失败后取消未完成请求；预处理改写错误按段落回退并继续。
- 所有写操作使用乐观修订号；后台失败和自动回退写入审计事件。
- Git 提交身份固定为 yjsnpi1145 <259851991+yjsnpi1145@users.noreply.github.com>。
- 本地服务运行时不执行固定端口的 tests/process/test_start_stop_scripts.py。

---

## File Structure

| Path | Responsibility |
|---|---|
| src/voice_pipeline/models/director.py | 预处理模式、阶段、段落请求与记录模型。 |
| src/voice_pipeline/models/director_llm.py | LLM 预处理结构单元和严格改写响应模型。 |
| src/voice_pipeline/modules/text/structural_cleaner.py | 全篇清洗、跨段落引号扫描、语义和停顿单元构造。 |
| src/voice_pipeline/modules/text/speakability.py | 共享可配音文本和纯停顿判断。 |
| src/voice_pipeline/core/director_preprocessing.py | 本地清洗、LLM 改写、回退、重试和确认编排。 |
| src/voice_pipeline/storage/orm.py | 项目文本层与预处理段落表。 |
| src/voice_pipeline/storage/director_store.py | 阶段、段落、审计和分页存储操作。 |
| src/voice_pipeline/storage/migrations/versions/0006_director_preprocessing.py | 0005 至 0006 的非破坏性架构迁移。 |
| src/voice_pipeline/modules/llm/{activity,runtime,client,fake}.py | LLM 改写、活动流和 fake 实现。 |
| src/voice_pipeline/modules/llm/script_chunking.py | 全篇引号安全切块和格式标点归并。 |
| src/voice_pipeline/core/director_analysis.py | 使用确认稿、翻译预检、并行失败取消。 |
| src/voice_pipeline/api/{app,director_routes}.py | 服务初始化、预处理 API、命令单飞。 |
| src/voice_pipeline/webui/{index.html,director.js,director-preprocessing.js,styles.css} | 模式选择、双栏校对、进度和操作。 |

---

### Task 1: 全篇结构清洗与可配音文本判定

**Files:**
- Create: src/voice_pipeline/modules/text/__init__.py
- Create: src/voice_pipeline/modules/text/speakability.py
- Create: src/voice_pipeline/modules/text/structural_cleaner.py
- Create: tests/unit/test_speakability.py
- Create: tests/unit/test_structural_cleaner.py

**Interfaces:**
- Produces is_speakable_text(value: str) -> bool and is_pause_marker(value: str) -> bool.
- Produces StructuralTextCleaner.clean(source_text: str) -> StructuralDocument.
- StructuralDocument exposes structural_text and paragraphs; each paragraph exposes stable ID, original range/text, structural text and structural units.
- StructuralUnit.context is quoted_dialogue, quote_bridge_narration, narration, formatting or pause_marker.

- [ ] **Step 1: Write failing speakability tests**

~~~python
from voice_pipeline.modules.text.speakability import is_pause_marker, is_speakable_text

def test_punctuation_only_text_is_never_speakable() -> None:
    assert not is_speakable_text('  “……”  ')
    assert not is_speakable_text('——')
    assert is_pause_marker('……')
    assert is_pause_marker(' ... ')

def test_unicode_letters_and_numbers_are_speakable() -> None:
    assert is_speakable_text('祥子，为什么——')
    assert is_speakable_text('Your Majesty')
    assert is_speakable_text('第10章')
~~~

- [ ] **Step 2: Write failing cleaner tests**

~~~python
source = '“我的初吻……”她慌乱地摆弄着手指，目光四处乱飘，“祥子，为什么——”'
document = StructuralTextCleaner().clean(source)
assert document.structural_text == source
assert [(unit.text, unit.context) for unit in document.paragraphs[0].units] == [
    ('“我的初吻……”', 'quoted_dialogue'),
    ('她慌乱地摆弄着手指，目光四处乱飘，', 'quote_bridge_narration'),
    ('“祥子，为什么——”', 'quoted_dialogue'),
]
~~~

Add exact cases for CRLF and excess blank lines, Japanese and ASCII paired quotes, a quote spanning the old 2400-character boundary, isolated closing quotes, and a line containing only an ellipsis. Assert two clean runs produce identical IDs and texts.

- [ ] **Step 3: Run tests and verify RED**

~~~powershell
$env:PYTHONPATH="$PWD\src"
& 'D:\TTSsystem\.venv\Scripts\python.exe' -m pytest tests/unit/test_speakability.py tests/unit/test_structural_cleaner.py -q
~~~

Expected: import failure because voice_pipeline.modules.text does not exist.

- [ ] **Step 4: Implement deterministic cleaner**

Use unicodedata.category in speakability.py; text is speakable only when a codepoint category starts with L or N. is_pause_marker accepts only whitespace and the set … . 。 、 ， , ! ！ ? ？ — - ~ ～.

In structural_cleaner.py normalize CRLF/CR to LF, trim empty leading/trailing paragraphs, collapse three-or-more newlines to two, and scan the complete normalized document with stacks for Chinese/Japanese paired quotes plus toggled ASCII quotes before any fixed-size split. Build quote, bridge, narration, formatting and pause-marker ranges. Attach formatting-only ranges to an adjacent semantic unit; retain an otherwise isolated pause paragraph as non-spoken pause_marker. Derive paragraph ID with:

~~~python
sha256(
    f"{ordinal}:{source_start}:{source_end}:{source_text}".encode("utf-8")
).hexdigest()
~~~

- [ ] **Step 5: Run tests and verify GREEN**

Run the Step 3 command. Expected: all tests pass.

- [ ] **Step 6: Commit**

~~~powershell
git add src/voice_pipeline/modules/text tests/unit/test_speakability.py tests/unit/test_structural_cleaner.py
git commit -m "feat: add deterministic director text cleaner"
~~~

---

### Task 2: 数据模型、SQLite 迁移与存储协议

**Files:**
- Modify: src/voice_pipeline/models/director.py
- Modify: src/voice_pipeline/storage/orm.py
- Create: src/voice_pipeline/storage/migrations/versions/0006_director_preprocessing.py
- Modify: src/voice_pipeline/storage/director_store.py
- Modify: tests/unit/test_director_models.py
- Modify: tests/integration_cpu/test_director_migration.py
- Modify: tests/integration_cpu/test_director_store.py

**Interfaces:**
- Adds statuses preprocessing and preprocess_review.
- Adds PreprocessMode = Literal["structural", "rewrite", "skip"].
- Adds project fields preprocessing_mode, structural_text, preprocessed_text and preprocess_revision.
- Adds DirectorPreprocessParagraphRecord and DirectorPreprocessParagraphPatch.
- Adds begin_preprocessing, stage_preprocess_document, save_preprocess_result, complete_preprocessing, list_preprocess_paragraphs, patch_preprocess_paragraph, restore_preprocess_paragraph, confirm_preprocessing and analysis_text store methods.

- [ ] **Step 1: Write failing model and migration tests**

Assert create request defaults to structural, paragraph patch rejects blank text, head revision is 0006_director_preprocessing, project columns contain all four new fields, and director_preprocess_paragraphs exists.

- [ ] **Step 2: Write failing store tests**

Create a project, begin preprocessing, stage a two-paragraph document, save its local paragraph results, complete preprocessing, and assert:

~~~python
assert project.status == "preprocess_review"
assert project.structural_text == expected_structural
assert project.preprocessed_text == expected_structural
assert page.total_count == 2
assert len(page.items) == 1
assert page.next_offset == 1
~~~

Patch one paragraph and assert current/project revisions increment and full preprocessed_text recomposes in ordinal order. Restore structural text, confirm, and assert status becomes analyzing. Assert stale revisions raise VERSION_CONFLICT.

- [ ] **Step 3: Run tests and verify RED**

~~~powershell
& 'D:\TTSsystem\.venv\Scripts\python.exe' -m pytest tests/unit/test_director_models.py tests/integration_cpu/test_director_migration.py tests/integration_cpu/test_director_store.py -q
~~~

Expected: missing fields/table/methods.

- [ ] **Step 4: Add schema and migration**

Add nullable structural_text and preprocessed_text, non-null preprocessing_mode default structural, and non-null preprocess_revision default 0 to director_projects. Define director_preprocess_paragraphs with paragraph_id primary key, project_id, ordinal, source range/text, structural/current text, rewrite_state, validation_json, revision, input/output SHA-256 values and timestamps. Add unique project/ordinal plus range/revision checks.

Migration 0006 must inspect before adding each field/table, backfill only mode/revision defaults, and must not convert legacy text. Downgrade removes the table and new fields.

- [ ] **Step 5: Implement revisioned store operations**

begin_preprocessing accepts draft, preprocess_review and retryable preprocessing; it clears last_error and logs preprocessing_started. stage_preprocess_document atomically replaces the project's paragraph rows and structural_text while keeping status preprocessing. save_preprocess_result is an internal service operation accepted only in preprocessing and updates one paragraph without exposing an editable review state. complete_preprocessing recomposes preprocessed_text, advances to preprocess_review, increments project/preprocess revisions and logs preprocessing_completed.

Patch/restore are accepted only in preprocess_review, update one paragraph, recompose full preprocessed_text, increment both revisions, and log edit/restore events. confirm_preprocessing rejects blank or non-speakable-only documents, advances to analyzing and logs preprocessing_confirmed. analysis_text returns preprocessed_text or source_text without mutating source_text.

- [ ] **Step 6: Run tests and verify GREEN**

Run the Step 3 command. Expected: all tests pass.

- [ ] **Step 7: Commit**

~~~powershell
git add src/voice_pipeline/models/director.py src/voice_pipeline/storage/orm.py src/voice_pipeline/storage/migrations/versions/0006_director_preprocessing.py src/voice_pipeline/storage/director_store.py tests/unit/test_director_models.py tests/integration_cpu/test_director_migration.py tests/integration_cpu/test_director_store.py
git commit -m "feat: persist director preprocessing drafts"
~~~

---

### Task 3: LLM 等义改写合同与预处理服务

**Files:**
- Modify: src/voice_pipeline/models/director_llm.py
- Modify: src/voice_pipeline/modules/llm/activity.py
- Modify: src/voice_pipeline/modules/llm/runtime.py
- Modify: src/voice_pipeline/modules/llm/client.py
- Modify: src/voice_pipeline/modules/llm/fake.py
- Create: src/voice_pipeline/core/director_preprocessing.py
- Create: tests/unit/test_director_preprocessing.py
- Modify: tests/unit/test_llm_client.py
- Modify: tests/unit/test_llm_activity.py

**Interfaces:**
- Adds PreprocessRewriteUnit, PreprocessRewriteItem and PreprocessRewriteResult.
- Adds RuntimeDirector.rewrite_preprocess_paragraph.
- Adds PreprocessingService.run and rewrite_paragraph.
- LLM activity operation is script_preprocessing.

- [ ] **Step 1: Write failing rewrite tests**

Use a capturing fake director. Assert structural mode makes zero LLM calls. In rewrite mode assert input IDs remain stable and output must provide the exact same ID order with input_unit_ids containing only its own unit ID. Parametrize missing, duplicate, reordered, blank, excessive-length, language-changing, protected-number-dropping and quote-wrapper-dropping responses; every invalid paragraph must keep structural text, persist rewrite_state fallback, expose validation details and still let the project reach preprocess_review.

- [ ] **Step 2: Write failing OpenAI contract/activity tests**

Assert request operation is script_preprocessing, payload includes paragraph ID and each unit ID/text/context, and system prompt explicitly forbids adding/deleting plot information and translating language. Assert the activity feed emits started, response, completed or failed without including secrets.

- [ ] **Step 3: Run tests and verify RED**

~~~powershell
& 'D:\TTSsystem\.venv\Scripts\python.exe' -m pytest tests/unit/test_director_preprocessing.py tests/unit/test_llm_client.py tests/unit/test_llm_activity.py -q
~~~

Expected: rewrite models/service/operation absent.

- [ ] **Step 4: Implement strict models and client**

~~~python
class PreprocessRewriteItem(StrictModel):
    unit_id: NonBlankText
    rewritten_text: PreservedNonBlankText
    input_unit_ids: tuple[NonBlankText, ...] = Field(min_length=1)

class PreprocessRewriteResult(StrictModel):
    items: tuple[PreprocessRewriteItem, ...] = Field(min_length=1)
~~~

OpenAiDirectorClient uses _schema_messages with exact one-to-one IDs. RuntimeDirector wraps the new call with its staged-call semaphore/counter and activity events. FakeDirector returns identity items.

- [ ] **Step 5: Implement validation, orchestration and fallback**

Validation requires exact ID order, input_unit_ids == (expected_id,), nonblank output, reasonable length ratio, stable Unicode script profile, preserved numbers/ASCII terms, and retained quote wrappers for quoted dialogue.

PreprocessingService begins the command, runs the cleaner once, stages all local paragraphs while the project remains preprocessing, and:
- structural: saves local results then completes the project;
- skip: stages source as structural/current then completes the project;
- rewrite: submits paragraphs through RuntimeDirector, saves valid output, catches per-paragraph PipelineError and saves structural fallback with details, then calls complete_preprocessing after all paragraphs settle.

Local cleaning/storage failures escape for project last_error; LLM paragraph errors do not block the local review draft. Rewriting one paragraph changes only that paragraph.

- [ ] **Step 6: Run tests and verify GREEN**

Run the Step 3 command. Expected: all tests pass.

- [ ] **Step 7: Commit**

~~~powershell
git add src/voice_pipeline/models/director_llm.py src/voice_pipeline/modules/llm/activity.py src/voice_pipeline/modules/llm/runtime.py src/voice_pipeline/modules/llm/client.py src/voice_pipeline/modules/llm/fake.py src/voice_pipeline/core/director_preprocessing.py tests/unit/test_director_preprocessing.py tests/unit/test_llm_client.py tests/unit/test_llm_activity.py
git commit -m "feat: add director LLM preprocessing rewrite"
~~~

---

### Task 4: 全篇引号安全分析、标点过滤与失败取消

**Files:**
- Modify: src/voice_pipeline/modules/llm/script_chunking.py
- Modify: src/voice_pipeline/core/director_analysis.py
- Modify: src/voice_pipeline/storage/director_store.py
- Modify: tests/unit/test_director_llm_stages.py
- Modify: tests/integration_cpu/test_director_analysis.py

**Interfaces:**
- split_script never divides a balanced full-document quote span.
- pause_marker materializes as stage_direction with speak_enabled false.
- ScriptAnalysisService consumes DirectorStore.analysis_text.
- Translation rejects spoken non-speakable rows and uses fail-fast batch awaiting.

- [ ] **Step 1: Write failing chunk and analysis tests**

Create an opening quote before index 2400 and closing quote after it; chunks must reconstruct exact text and keep the quote whole. Create standalone closing quote, ellipsis and blank lines; no spoken unit may lack a Unicode letter/number, and isolated ellipsis must be a non-spoken stage direction.

Create a project whose preprocessed_text differs from source, confirm preprocessing, analyze, and assert concatenated utterance source_text equals confirmed preprocessed_text. Mark punctuation-only text spoken and assert typed INVALID_INPUT before any translation call. Make the second parallel batch raise and assert pending tasks receive cancellation.

- [ ] **Step 2: Run tests and verify RED**

~~~powershell
& 'D:\TTSsystem\.venv\Scripts\python.exe' -m pytest tests/unit/test_director_llm_stages.py tests/integration_cpu/test_director_analysis.py -q
~~~

Expected: local chunk boundaries split quotes, analysis reads source_text, and translation accepts punctuation/allows sibling requests to continue.

- [ ] **Step 3: Implement safeguards**

Build balanced quote spans over the entire analysis text before choosing chunk boundaries. If a preferred boundary is inside a quote, select the latest safe boundary before its opener; if one quote exceeds max_chars, keep that quote as one oversized chunk.

Merge non-speakable formatting ranges into neighbours. Extend analysis context with pause_marker and constrain it to non-spoken stage direction. Analyze analysis_text(project), not the immutable source.

Add _gather_fail_fast: create tasks, await FIRST_EXCEPTION, cancel and await pending tasks, then re-raise. Use it for analysis and translation batches. Validate every speak_enabled working_text with is_speakable_text before building TranslationInput.

- [ ] **Step 4: Run tests and verify GREEN**

Run the Step 2 command. Expected: all tests pass.

- [ ] **Step 5: Commit**

~~~powershell
git add src/voice_pipeline/modules/llm/script_chunking.py src/voice_pipeline/core/director_analysis.py src/voice_pipeline/storage/director_store.py tests/unit/test_director_llm_stages.py tests/integration_cpu/test_director_analysis.py
git commit -m "fix: make director analysis quote-safe and speakable"
~~~

---

### Task 5: 预处理 API、恢复与后台命令单飞

**Files:**
- Modify: src/voice_pipeline/api/app.py
- Modify: src/voice_pipeline/api/director_routes.py
- Modify: src/voice_pipeline/storage/director_store.py
- Modify: tests/contract/test_director_api.py
- Modify: tests/process/test_director_restart.py

**Interfaces:**
- POST /api/v1/director-projects/{id}/preprocess
- GET /api/v1/director-projects/{id}/preprocess?offset=0&limit=20
- PATCH /api/v1/director-projects/{id}/preprocess-paragraphs/{paragraph_id}
- POST /api/v1/director-projects/{id}/preprocess-paragraphs/{paragraph_id}/restore
- POST /api/v1/director-projects/{id}/preprocess-paragraphs/{paragraph_id}/rewrite
- POST /api/v1/director-projects/{id}/confirm-preprocessing
- ControlPlane.director_commands maps (project_id, operation) to one asyncio task.

- [ ] **Step 1: Write failing API tests**

Create rewrite-mode project, submit preprocess, poll preprocess_review, read page limit 1, patch, restore, rerun rewrite, then confirm and poll role_review. Assert stale revision is 409 and blank edit is 422.

Submit preprocess twice against a blocking fake service and assert both return 202 while service call count is one. Repeat the single-flight assertion for analyze and translate.

- [ ] **Step 2: Write failing restart test**

Create projects in preprocessing, analyzing and translating. Restart and assert each receives DIRECTOR_COMMAND_INTERRUPTED and none automatically calls LLM.

- [ ] **Step 3: Run tests and verify RED**

~~~powershell
& 'D:\TTSsystem\.venv\Scripts\python.exe' -m pytest tests/contract/test_director_api.py tests/process/test_director_restart.py -q
~~~

Expected: endpoints, command map and preprocessing recovery are absent.

- [ ] **Step 4: Wire service, routes and single-flight**

Instantiate PreprocessingService during app lifespan. The list endpoint returns items, total_count and next_offset. Patch/restore/rewrite call store/service with project and paragraph revision checks. Confirm changes to analyzing then spawns ScriptAnalysisService using returned revision.

Refactor _spawn to key tasks by project and operation. If a non-completed task exists, close the unused coroutine and return the active task status; do not start another request. Remove the key only in its done callback. Shutdown cancels all mapped tasks. recover_interrupted_commands includes preprocessing.

- [ ] **Step 5: Run tests and verify GREEN**

Run the Step 3 command. Expected: all tests pass.

- [ ] **Step 6: Commit**

~~~powershell
git add src/voice_pipeline/api/app.py src/voice_pipeline/api/director_routes.py src/voice_pipeline/storage/director_store.py tests/contract/test_director_api.py tests/process/test_director_restart.py
git commit -m "feat: expose director preprocessing workflow"
~~~

---

### Task 6: WebUI 预处理校对界面

**Files:**
- Modify: src/voice_pipeline/webui/index.html
- Create: src/voice_pipeline/webui/director-preprocessing.js
- Modify: src/voice_pipeline/webui/director.js
- Modify: src/voice_pipeline/webui/director-llm-activity.js
- Modify: src/voice_pipeline/webui/styles.css
- Create: tests/unit/test_director_preprocessing_js.py
- Modify: tests/unit/test_director_llm_activity_js.py
- Modify: tests/integration_cpu/test_webui_workbench.py

**Interfaces:**
- Pure helpers preprocessDraftState, canConfirmPreprocessing, nextPreprocessOffset and preprocessStatusLabel.
- Unsaved preprocessing drafts remain separate from utterance and translation drafts.
- directorOperationLabels.script_preprocessing equals 文本预处理.

- [ ] **Step 1: Write failing JS tests**

~~~javascript
const paragraph = {paragraph_id:'p1', current_text:'清洗稿', structural_text:'清洗稿'};
assert(preprocessDraftState(paragraph, '用户稿').dirty === true);
assert(preprocessDraftState(paragraph, '清洗稿').dirty === false);
assert(canConfirmPreprocessing({status:'preprocess_review'}, new Map()) === true);
assert(canConfirmPreprocessing(
  {status:'preprocess_review'}, new Map([['p1', '未保存']])
) === false);
assert(nextPreprocessOffset({next_offset:20}) === 20);
assert(nextPreprocessOffset({next_offset:null}) === null);
~~~

Extend activity fixtures with script_preprocessing and assert it remains visible with label 文本预处理.

- [ ] **Step 2: Run tests and verify RED**

~~~powershell
& 'D:\TTSsystem\.venv\Scripts\python.exe' -m pytest tests/unit/test_director_preprocessing_js.py tests/unit/test_director_llm_activity_js.py -q
~~~

Expected: helper module and activity label absent.

- [ ] **Step 3: Add creation and review UI**

Add selector values structural, rewrite and skip; rename submit to 创建并开始预处理. Add director-preprocess-review between LLM monitor and role area with sticky metrics, scrollable lazy paragraph cards and confirm action.

On create, POST project then POST preprocess. For preprocessing/review statuses load the first page and append following pages through IntersectionObserver. Persist selected project, paragraph scroll and unsaved paragraph draft map in sessionStorage.

Each card contains read-only original, editable current text, state chip, save, restore original, restore local clean, rerun LLM and diff details. Confirmation is disabled while a draft is unsaved or status is not preprocess_review. Role and timeline panels show waiting state until analysis has published roles.

- [ ] **Step 4: Add responsive styles and package assertion**

Use two equal columns above 900px and one column below, capped editor height, dark-console contrast, warning colour for fallback, and sticky summary. Import the helper with the current cache-busting token. Assert director-preprocessing.js is served and later packaged.

- [ ] **Step 5: Run tests and JS checks**

~~~powershell
& 'D:\TTSsystem\.venv\Scripts\python.exe' -m pytest tests/unit/test_director_preprocessing_js.py tests/unit/test_director_llm_activity_js.py tests/integration_cpu/test_webui_workbench.py -q
node --check src/voice_pipeline/webui/director.js
node --check src/voice_pipeline/webui/director-preprocessing.js
~~~

Expected: all commands exit 0.

- [ ] **Step 6: Commit**

~~~powershell
git add src/voice_pipeline/webui/index.html src/voice_pipeline/webui/director-preprocessing.js src/voice_pipeline/webui/director.js src/voice_pipeline/webui/director-llm-activity.js src/voice_pipeline/webui/styles.css tests/unit/test_director_preprocessing_js.py tests/unit/test_director_llm_activity_js.py tests/integration_cpu/test_webui_workbench.py
git commit -m "feat: add director preprocessing review UI"
~~~

---

### Task 7: 端到端回归、质量门、发布与本地验收

**Files:**
- Modify: tests/integration_cpu/test_director_end_to_end.py
- Modify: README.md
- Modify: CHANGELOG.md

**Interfaces:**
- Verifies source → preprocessing review → confirmation → role review → translation → generation.
- Documents structural and rewrite modes plus local fallback.

- [ ] **Step 1: Write end-to-end regression**

Use the mixed dialogue sentence plus isolated ellipsis. Run structural preprocessing, edit one paragraph, confirm, analyze, confirm roles, translate and generate. Assert immutable source remains exact, analysis uses confirmed preprocessed text, bridge narration is separate, no punctuation-only row is spoken, and all generated items contain text.

- [ ] **Step 2: Run focused suite**

~~~powershell
$env:PYTHONPATH="$PWD\src"
& 'D:\TTSsystem\.venv\Scripts\python.exe' -m pytest tests/unit/test_speakability.py tests/unit/test_structural_cleaner.py tests/unit/test_director_models.py tests/unit/test_director_preprocessing.py tests/unit/test_director_llm_stages.py tests/unit/test_llm_client.py tests/unit/test_llm_activity.py tests/unit/test_director_preprocessing_js.py tests/unit/test_director_llm_activity_js.py tests/integration_cpu/test_director_migration.py tests/integration_cpu/test_director_store.py tests/integration_cpu/test_director_analysis.py tests/integration_cpu/test_director_end_to_end.py tests/contract/test_director_api.py -q
~~~

Expected: zero failures.

- [ ] **Step 3: Update documentation**

Document create → preprocessing → manual confirmation → role/translation review → generation. Describe local cleaning, optional rewrite, per-paragraph fallback, original retention and punctuation filtering. Add matching CHANGELOG entry.

- [ ] **Step 4: Run all quality gates**

~~~powershell
& 'D:\TTSsystem\.venv\Scripts\python.exe' -m ruff check src tests
& 'D:\TTSsystem\.venv\Scripts\python.exe' -m mypy src/voice_pipeline workers
& 'D:\TTSsystem\.venv\Scripts\python.exe' -m pytest -m "not gpu and not gpu_residency and not quality_model and not process and not crash_recovery" -q
uv build --wheel
& 'D:\TTSsystem\.venv\Scripts\python.exe' -c "import glob,zipfile; p=glob.glob('dist/*.whl')[-1]; z=zipfile.ZipFile(p); assert any(n.endswith('director-preprocessing.js') for n in z.namelist()); print(p)"
git diff --check origin/main...HEAD
~~~

Expected: every command exits 0.

- [ ] **Step 5: Commit regression and docs**

~~~powershell
git add tests/integration_cpu/test_director_end_to_end.py README.md CHANGELOG.md
git commit -m "test: cover director preprocessing workflow"
~~~

- [ ] **Step 6: Push, merge, deploy and accept**

~~~powershell
git push -u origin codex/director-text-preprocessing-design
gh pr create --base main --head codex/director-text-preprocessing-design --title "feat: add director text preprocessing review" --body-file docs/superpowers/specs/2026-08-28-director-text-preprocessing-design.md
gh pr checks --watch
gh pr merge --squash --delete-branch
git fetch origin
~~~

Install the merged wheel into D:\TTSsystem\.venv-control, restart with scripts/stop.ps1 and scripts/start.ps1, then verify health and root return HTTP 200, director project list is accessible, a smoke project reaches preprocess_review, and WebUI displays mode selector, review cards, LLM monitor and confirmation action.
