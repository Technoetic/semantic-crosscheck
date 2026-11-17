# 단계·옵션·종료 코드

## v0.28.0 선택형 독립 검산·계획 비교

| 단계 | 입력과 범위 | 단계별 옵션 | 주요 산출물 |
|---|---|---|---|
| `measure-oracle` | 한 테이블 원시 행의 독립 SUM/COUNT와 Cube 지표 비교. [명세와 한계](../measure-oracle.md). | `--spec FILE`; `--config PATH` 생략 시 `./crosscheck.toml` 필수 | `measure_oracle/result.json` |
| `plan-compare` | 임시 합성 데이터에서 실제 다른 계획과 전체 행 비교. `--config` 불가. [명세와 한계](../plan-compare.md). | `--spec FILE`, `--dsn-env SXC_TEST_ADMIN_DSN`, `--statement-timeout-ms 1000` | `plan_compare/result.json` |

둘 다 `--out DIR`·`--quiet`를 지원하고, counts-only `measure_oracle`/`plan_compare` 결과를 쓴다. 기본 report 판정에는 포함되지 않는다. 두 진단의 종료 코드 0은 명시한 범위의 비교 성공, 1은 불일치·미실행(`not_run`), 2는 오류·사용법·명세 오류다. 현재 `plan-compare`의 DSN 누락·연결 실패는 `not_run`(종료 1), `measure-oracle`의 Cube·원시 조회 환경 부족은 오류(종료 2)로 처리한다. 아래 공통 종료 코드 3의 환경 부족 규약을 두 진단에 그대로 적용하지 않는다.

이 문서는 README의 '단계·옵션·종료 코드' 절과 선택 진단 절의 참조 문서입니다(v0.28.0). 설치와 첫 실행은 [README의 빠른 시작](../../README.md#quick-start-with-the-demo)에 있습니다.


```text
python skills/semantic-crosscheck/scripts/sxc.py <stage> [--config PATH] [--out DIR] [--quiet] [options]
```

| 단계 | 역할 | 단계별 옵션 | 주요 산출물 |
|---|---|---|---|
| `doctor` | Python·pg8000·DSN·PostgreSQL·검사 역할 권한·선택 구성 요소 확인 | `--require core,reason,cube` | `doctor/result.json` |
| `demo` | loader 역할로 가상 데이터 적재 | `--variant clean\|dirty`, `--seed N`, `--scale N` | `demo/result.json`, `demo/expected.json` |
| `build` | 위반 뷰·계층 폐포 생성 | `--hierarchy NAME` | `build/result.json`, `build/build.sql`, `build/shapes.ttl`, `build/fk_review.sql` |
| `reason` | SHACL ↔ SQL, OWL 일관성·폐포·분류 대조 | `--only shacl,owl`, `--synthetic-witnesses` | `reason/result.json` |
| `cube` | Cube 모델 생성·시작·검사 | 별도 옵션 없음 | `cube/result.json`, `cube/model/*.yml`, `cube/model.json`, `cube/server.log` |
| `verify` | V1·V2·V3·V4 검증 | `--only v1,v2,v3,v4`, `--gold ID` | `verify/result.json`, `verify/server.log`, `verify/mutants/*/` |
| `report` | 필수 단계의 실행·최신성·통과 여부 종합 | `--require-doctor` | `report/result.json`, `report.md`, `report.json` |
| `cube-install` | 사용자별 고정 Cube 어댑터 설치 | `--home DIR`, `--native-file PATH`, `--force` | `cube_install/result.json`, `cube_install/npm-ci.log`, `cube_install/smoke.log` |
| `counterexample` | 합성 데이터에서 SQL의 차이·BOM 변환 관계를 검사하고 축소·재현 | `--reference-sql FILE`, `--candidate-sql FILE`, `--metamorphic-sql FILE`, `--mutation-operator NAME`, `--gold-sql FILE`, `--scenario nulls`, `--seed N`, `--max-examples N`, `--max-shrink-steps N`, `--shrink-strategy structured`, `--witness-schema FILE`, `--solver-timeout-ms N`, `--dsn-env NAME`, `--statement-timeout-ms N`, `--constraints FILE`, `--constraint-audit FILE`, `--replay FILE` | `counterexample/result.json`, `counterexample/fixture.json` |
| `cube-replay` | 고정 합성 데이터를 실제 Cube 원형·변형 모델과 독립 gold SQL로 비교 | `--mutation-operator NAME`, `--allow-scratch-schema`, `--seed N`, `--dsn-env NAME`, `--statement-timeout-ms N`, `--replay FILE` | `cube_replay/result.json`, `cube_replay/fixture.json` |
| `boundary-schema` | 선언한 단일 스칼라 SQL 비교의 정확한 경계값·NULL에서 기존 witness-v1 스키마를 생성하는 오프라인 실험 | `--spec FILE` | `boundary_schema/result.json`, `boundary_schema/witness-schema.json` |
| `semantic-preflight` | Offline authored business-definition and structured-plan consistency | `--contract FILE`, `--plan FILE`; explicit `--config FILE` required | `semantic_preflight/result.json` |
| `sql-structure` | Optional offline PostgreSQL17 AST comparison with authored structural expectations | `--sql FILE`, `--expect FILE`; optional `--out DIR`, `--quiet`; no `--config` | `sql_structure/result.json` |
| `tlp` | 참·거짓·NULL 분할 검사. 0.10.0부터 선택형 NoREC 조건 건수 비교 | `--spec FILE`, `--oracle tlp\|norec`, `--dsn-env NAME`, `--seed N`, `--max-examples N`, `--statement-timeout-ms N`, `--replay FILE`, `--shrink`, `--max-shrink-steps N` | `tlp/result.json`, `tlp/fixture.json` |
| `shacl-corpus` | 독립 기대 결과와 pySHACL·Apache Jena의 합성 보고서 비교. 0.9.0부터는 W3C SHACL 시험 모음 core의 프로필 안 사례도 W3C 기대 보고서와 비교 | `--jena-home DIR`, `--timeout-ms N`, `--w3c-suite [DIR]`, `--allow-unpinned-suite` | `shacl_corpus/result.json` |
| `expect` | 전용 fixture DB에서 gold SQL·Cube를 실행해 `[[gold.expect]]`의 문자 그대로 기대 행과 비교 | `--allow-fixture-database`, `--dsn-env NAME` | `expect/result.json`, `expect/build/`, `expect/cube/`, `expect/server.log` |
| `data-rules` | 데이터 규칙을 PostgreSQL과 독립 Python으로 각각 평가해 같은지 확인하고, 규칙별 뷰와 문제 품번 뷰를 검증 | `--rule ID` | `data_rules/result.json`, `data_rules/rules.sql` |

나머지 네 단계 `shacl-repair`·`grain`·`okf`·`knowledge-evolution`의 옵션은 영문 부록의 [Optional diagnostic contracts](#optional-diagnostic-contracts) 표에 있습니다.

**공통 옵션**

- `--config PATH`: 명시하면 반드시 존재해야 합니다. 없으면 exit 2이며 단계를 실행하지 않습니다. 생략하면 `./crosscheck.toml`을 찾습니다. `doctor`·`cube-install`은 설정 없이도 실행합니다. 별도 합성 도구 `counterexample`은 설정을 읽지 않으며 `--config`를 거부합니다. `expect`는 운영 설정을 읽지만 fixture DB에만 접속합니다.
- `--out DIR`: 기본 출력 위치는 설정 옆 `.sxc`입니다. 설정 없이 실행하면 `./.sxc`이며, 새 출력 디렉터리에는 내용 `*`의 `.gitignore`를 만듭니다.
- `--quiet`: 요약 한 줄만 출력합니다. `sxc.py --version`은 버전을 표시합니다.

| 종료 코드 | 뜻 |
|:---:|---|
| **0** | 통과 |
| **1** | 검사 실패·오류·미실행, 또는 `--only`·`--gold`·`--hierarchy`·`--rule`로 범위를 좁힌 실행 |
| **2** | 설정·사용법 오류 |
| **3** | DSN·DB 버전·Python 패키지·Java·Node.js·Cube 어댑터 등 환경 부족 |

[설정 레퍼런스](../../skills/semantic-crosscheck/references/config.md) · [전체 설계 계약](../../docs/design.md)

<a id="offline-business-definition-preflight"></a>
## Offline business-definition preflight

[`semantic-preflight`](../../docs/semantic-preflight.md) optionally compares authored
metric definitions and a structured plan with configured measure/Gold/Cube
metadata. It requires explicit `--config`, `--contract` and `--plan`; it reads no
Gold/revision SQL body and makes no DB, network or model call.

```text
python skills/semantic-crosscheck/scripts/sxc.py semantic-preflight --config examples/semantic-preflight/crosscheck.toml --contract examples/semantic-preflight/contract.json --plan examples/semantic-preflight/plan.json --out .sxc-semantic-preflight
```

**`reviewed` is an unauthenticated caller assertion. Matching unit, population,
denominator and time fields do not prove SQL meaning; a plan period does not
bind SQL parameters or prove date filtering.** Missing or ambiguous selections
remain `not_run`; complete conflicts fail. This adds no default stage or report
authority unless paired `[semantic_preflight]` sidecars are explicitly configured; see [report evidence](../../docs/contract-report.md). The new Python engine code
changes the existing fingerprint, so rerun operational stages after upgrading.
See the [schema, synthetic example and reused NVIDIA references](../../docs/semantic-preflight.md).

<a id="optional-diagnostic-contracts"></a>
## Optional diagnostic contracts (since v0.13.0)

Three default-off paths deepen the evidence around a failure:

- **SHACL repair fixtures:** `shacl-repair --spec FILE` runs real pySHACL on independently authored before/after synthetic graphs, compares normalized expected report bags and verifies retained targets and immutable facts. Separate edit costs do not override hard preservation. [Schema and examples](../../docs/shacl-repair.md).
- **Independent grain contracts:** `grain --config FILE --spec FILE` compares authored source/group/output/key/filter/null-policy claims with the existing undated preaggregate-to-node operation, then checks actual key and join preconditions in one bounded read-only PostgreSQL snapshot. [Schema and examples](../../docs/grain.md).
- **OWL explanations:** `reason --explain-inconsistency` localizes generated axiom groups with the same HermiT after a consistency failure, within axiom/count/time caps. Only completed retained-deletion checks establish subset minimality; the original failure stays. Since 0.14.0, a completed explanation also reports `uniqueness` (`unique`, `not_unique` or `not_proven`) from one extra full-set check per retained group within the same caps; `unique` covers only explanations over the generated groups with this HermiT. [Limits and examples](../../docs/owl-explanations.md).

The first two are standalone diagnostics outside the default report pipeline. They do not prove business truth, complete SHACL report equivalence, independent OWL correctness, global-minimum explanations or future data consistency. Runtime dependencies and defaults are unchanged; the optional diagnostics are included since v0.13.0 (`uniqueness` since v0.14.0).

| Stage | Additional diagnostic options |
|---|---|
| `shacl-repair` | `--spec FILE`, `--timeout-ms 30000` |
| `grain` | `--spec FILE`, `--max-rows 100000` |
| `okf` | Since v0.15.0, metadata only: `--export DIR --select GOLD_ID` (repeatable selection) or `--import-preview DIR`; explicit `--config FILE` required |
| `knowledge-evolution` | Since v0.15.0, fixed offline synthetic replay: `--out DIR`, `--quiet`; rejects `--config`, accepts no external fixture; no engines |

The [offline knowledge evolution diagnostic](../../docs/knowledge-evolution.md)
checks five fixed source transitions and 26 contracts through actual source and
report APIs. Restoration leaves retained expectation evidence `not_run`; retiring
the authored case changes the configuration fingerprint and stales old synthetic
operational receipts. PASS measures only harness behavior, with no semantic
coverage, DB, engine or model execution. Together with
[authored source binding](../../docs/knowledge-sources.md) and
[selected OKF metadata exchange](../../docs/okf.md), it remains opt-in and outside
operational report gates. All three are opt-in additions available since v0.15.0.

The opt-in reason diagnostic accepts `--explain-inconsistency`, `--explain-max-axioms 128`, `--explain-max-checks 64`,
`--explain-timeout-ms 10000`, `--explain-max-explanations 1` (1..32) and `--explain-max-search-nodes 256` (1..4096).
For example, request up to five alternative causes with
`reason --explain-inconsistency --explain-max-explanations 5 --explain-max-checks 256 --explain-timeout-ms 60000`.
Each reported alternative is a verified subset-minimal generated-group core. `enumeration_status: partial` means
the result, node, call or time budget interrupted the search; only an exhausted search or established first-core
uniqueness gives `complete`. Original consistency failure and first-core fields remain unchanged.

V4 now replays every eligible chunk of a bounded V3 pair baseline and its NULL group, using the same in-memory key
partition. Chunk replay item ids append `:chunk[<index>]` to the V3 check id; the NULL group appends `:null`.
These are suite item ids in `killed_by` and coverage, not additional top-level checks. The configurable
`verify.mutant_max_items` (positive integer, default 10000) caps logical replay items per mutant. Without an
earlier non-pass, exhaustion is `not_run` (`suite_budget`), not a survival verdict. Since v0.24.0 optional `all`
observation retains an earlier non-pass and records later budget exhaustion as partial observations.
`verify/mutation-coverage.json` records each selected mutant
instance, executed items/attempt counts and unexecuted reasons, including equivalent-on-current-data and
not-applicable instances. Mutant details also retain their execution receipts. An early confirmed kill remains
valid with remaining items explicitly unrun in default `first` mode; optional `all` observes later items within
the budget. Above `pair_key_budget`, sampling and its disclosures remain;
Cube queries and SQL still do not share an MVCC snapshot. See [replay coverage](../../docs/mutation-coverage.md).
