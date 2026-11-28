# 검사를 추가하면 어떤 오류를 더 찾을까?

두 쿼리가 모두 수량을 20으로 계산하면 결과끼리 비교하는 검사는 통과한다. 하지만 원래 수량이 3과 7이고, 태그 두 개가 붙으면서 각 수량이 두 번 집계됐다면 올바른 답은 10이다. 이 예제는 두 쿼리의 결과에 직접 작성한 기대값까지 대조했을 때 어떤 사례의 판정이 달라지는지 보여준다.

## 결과를 재현하기

소스 저장소의 루트에서 Python 3.11 이상으로 실행한다.

```sh
python tools/run_paired_example.py
python tools/paired_release.py --plan examples/paired-release/plan.json --baseline examples/paired-release/baseline.json --candidate examples/paired-release/candidate.json --claims examples/paired-release/claims.json --check-markdown docs/releases/v0.30.0.md
```

첫 명령은 합성 데이터로 두 구성을 다시 실행하고, 나온 결과가 저장된 두 JSON 파일과 바이트 단위로 일치하는지 확인한다. 두 번째 명령은 원본 실행 기록에서 사례별 변화를 집계하고 릴리스 노트의 표와 대조한다. JSON 출력에서 `groups.heldout.case_ids.added_detections`를 열면 새로 찾아낸 사례의 ID가 보인다. [각 사례의 쿼리와 데이터](../examples/paired-release/README.md)

## 새 실험에 적용하기

1. 먼저 [계획 파일](../examples/paired-release/plan.json)에 기준 검사 목록, 추가할 검사 하나, 양쪽에 허용할 사례 시도 횟수, 사례별 정답과 노출 여부, 입력 파일의 SHA-256을 고정한다. 발표할 지표도 `registered_claims`에 등록해 결과를 본 뒤 불리한 항목을 빼는 것을 막을 수 있다. 결과를 열어 보기 전에 계획과 코드의 커밋을 남긴다.
2. 같은 사례를 양쪽 구성에 실행한다. 기록은 각 사례의 ID, 입력 해시와 상태를 담는다. `flag`는 문제 검출, `clear`는 검사 통과다. 실행 오류(`error`)나 미실행(`not_run`)이 어느 한쪽에라도 있으면 그 쌍은 판정 불가다. 실행 오류는 시도 횟수에 포함한다.
3. 두 결과 파일을 `--baseline`과 `--candidate`로 넘긴다. 정답이 오류인 사례에서 통과 → 검출은 추가 검출, 검출 → 통과는 기존 검출 손실이다. 정답이 정상인 사례에서 같은 변화는 각각 새 오탐, 기존 오탐 해소다. JSON의 `pairs`에서 양쪽 원래 판정을 확인할 수 있다.
4. 발표할 숫자를 `--claims`에 전달하고 문서는 `--check-markdown`으로 지정한다. 문서에는 [릴리스 노트](releases/v0.30.0.md)와 같은 표를 `paired-claims:begin`과 `paired-claims:end` 주석 사이에 둔다. 종료 코드 0은 입력 검증과 요청한 대조가 통과했음을 뜻한다. 수치 불일치는 1, 누락되거나 잘못된 입력은 2다.

검사 설계에 사용한 사례는 `development`, 검사 선택 후 따로 보관한 사례는 `heldout`, 이전 노출 여부를 모르면 `unknown`으로 기록한다. 이미 보거나 튜닝에 사용한 자료는 그 사실을 실험 설명에 남긴다. 이 도구는 선언한 노출 여부나 업무 정답의 정확성을 인증하지 않는다.

이번 지표는 같은 사례를 같은 횟수만큼 시도한 결과다. 실행 시간과 컴퓨팅 비용은 측정하지 않았다. 이 실험의 SQL은 메모리 내 SQLite에서 실행하고, 판정에는 플러그인의 실제 행 비교 함수를 사용한다. 일반 사용자의 기본 보고서와 운영 데이터 판정은 별개다.

## 소표본 통계 해석과 실험 설계 기준 (v0.31.0)

`tools/paired_release.py`는 각 노출 집단(`heldout`, `development`, `unknown`)의 확정 쌍에 대해 `groups.<exposure>.statistics` 아래 `detections`(`expected == "error"`)와 `false_positives`(`expected == "clean"`)의 쌍체 통계를 Python 표준 라이브러리(`math.comb`, `statistics.NormalDist`)만으로 함께 계산한다.

- **쌍체 가설 검정:** 정확 조건부 McNemar 양측 p-값(`exact_mcnemar_p`)과 Mid-P McNemar 양측 p-값(`midp_mcnemar_p`)을 함께 출력한다([Fagerland, Lydersen & Laake 2013](https://doi.org/10.1186/1471-2288-13-91)). 소표본 이진 쌍체 자료에서 정확 조건부 검정은 보수적이며 Mid-P가 검정력을 보완한다.
- **비율 차 95% 신뢰구간:** 후보와 기준의 비율 차(`rate_diff = (b - c) / n`)에 대해 쌍체 Wilson 점수 95% 신뢰구간(`diff_ci_95`, Method 10)을 계산한다([Newcombe 1998](https://doi.org/10.1002/%28SICI%291097-0258%2819981130%2917:22%3C2635::AID-SIM954%3E3.0.CO;2-C)).
- **최소 표본 공시:** 기존 검출 손실이 `0`(`c = 0`)이어도 양측 `p < 0.05`에 도달하려면 최소 `b >= 5`(Mid-P `0.03125`) 또는 `b >= 6`(정확 `0.03125`)의 불일치 쌍이 필요하다. 불일치 쌍(`discordant = b + c`)이 이 하한보다 작으면 `underpowered_for_midp_005` / `underpowered_for_exact_005`가 `true`로 표시된다.
- **v0.30.0 예시 해석:** v0.30.0의 `heldout` 오류 사례 3건(`a=1, b=2, c=0, d=0`)은 `exact_mcnemar_p = 0.5`, `midp_mcnemar_p = 0.25`, `diff_ci_95 = [-0.058569, 0.938508]`, `underpowered_for_midp_005 = true`다. 따라서 이 수치는 추가 검사의 결정적 동작 시연이며 통계적 유의성 근거가 아니다.
- **실험 위생:** 동일 시험 집합에서 두 검출기를 비교할 때 쌍체 검정(McNemar 계열)을 선택하는 근거([Dietterich 1998](https://doi.org/10.1162/089976698300017197)), 무작위 생성기·변이 비교 시 비모수 검정과 효과 크기 병기 원칙([Arcuri & Briand 2014](https://doi.org/10.1002/stvr.1486)), 합성 결함 중복 제거·시드 고정·개발 사례 노출 분리 규칙([Klees et al., CCS 2018](https://doi.org/10.1145/3243734.3243804))을 따른다. 같은 스키마에서 파생된 유사 합성 사례를 독립 표본으로 과대 해석하지 않는다.

## 순차 모니터링용 상시 유효 e-값 (v0.32.0)

사례가 점진적으로 추가되는 held-out 코퍼스에서 결과를 반복 확인할 때(optional stopping / optional continuation) 고정 표본 McNemar p-값은 제1종 오류율이 팽창한다. v0.32.0부터 `tools/paired_release.py`는 표준 라이브러리(`math.lgamma`)만으로 불일치 쌍 $(b, c)$에 대한 정확한 Jeffreys $\mathrm{Beta}(1/2, 1/2)$ 혼합 마팅게일 e-값을 함께 출력한다([Grünwald, de Heide & Koolen, JRSS-B 2024](https://doi.org/10.1093/jrsssb/qkae011); [Ramdas et al., Statistical Science 2023](https://doi.org/10.1214/23-STS894); [Howard et al., Annals of Statistics 2021](https://doi.org/10.1214/20-AOS1991)).

- **출력 필드:** `safe_e_value`(오버플로 방지 상한 $\ln E > 700$ 초과 시 `null`), `safe_e_log`(정확한 자연로그 $\ln E$, 독립 코호트 합산용), `safe_anytime_p`($\min(1, 1/E)$), `min_discordant_for_e_005 = 7`, `underpowered_for_e_005`.
- **임계 기준:** 기존 손실 `c = 0`에서 상시 유효 유의수준 `0.05`($E \ge 20$)에 도달하려면 최소 `b >= 7`의 불일치 쌍이 필요하다(`b = 6, c = 0`은 `E = 14.4375`, `b = 7, c = 0`은 `E = 26.8125`, `safe_anytime_p = 0.037296`). v0.30.0 예시 `(b = 2, c = 0)`은 `safe_e_value = 1.5`, `safe_anytime_p = 0.666667`, `underpowered_for_e_005 = true`다.
- **코호트 결합:** 서로 독립적인 held-out 코호트의 근거는 각 `safe_e_log`를 더해($E_{\text{total}} = \prod E_i$) 제1종 오류 보장을 유지한 채 결합할 수 있다.

## 사전 등록 핵심 봉인·사후 개정 이중 공시 및 발동 감사 (v0.33.0)

[`ai3inc/foldboost`](https://github.com/ai3inc/foldboost)(`v0.3.0`, 커밋 `ff7ad2208cf52d7692d5d016ba5b788d782256c7`, Apache-2.0)의 실험 규율 패턴을 Python 표준 라이브러리만으로 `tools/paired_release.py`와 `tools/run_paired_example.py`에 선택 반영했다(기존 v0.30.0–v0.32.0 입력·출력 하위호환).

- **사전 등록 핵심 봉인과 `claim_limit`:** `python tools/paired_release.py --plan plan.json --seal-plan`으로 불변 사전 등록 핵심 필드(`schema_version`, `experiment`, `added_check`, `baseline_checks`, `budget`, `cases`, 선택형 `registered_claims`·`claim_limit`·`frozen_utc`)의 정규화 SHA-256(`frozen_core_sha256`)을 계산할 수 있다. `plan.json`에 `amendments[]`(`timing: pre_measurement | post_hoc`, `recorded_utc`, `what_changed`, `why`, 선택형 `exclude_case_ids`)를 덧붙이려면 `frozen_core_sha256`이 필수이며, `pre_measurement` 개정이 영수증 `measured_utc`보다 늦거나 `measured_utc`가 `frozen_utc`보다 이르면 종료 코드 `2`로 거부한다.
- **이중 판정 공시와 `--strict-original`:** `amendments`가 존재해도 `groups`와 `pairs`는 원래 사전 등록 전체 사례의 집계(McNemar·Wilson CI·Safe e-value)를 그대로 유지하며, `amendment_audit` 안에 `original_groups`와 `amended_groups` 및 `post_hoc_altered_summary`를 나란히 기록한다. `--strict-original`은 사후(`post_hoc`) 개정이 원 집계를 변경하면 종료 코드 `1`로 실패한다.
- **처치군 발동 · 대조군 0-누출 감사 (`--check-activation`, 종료 코드 `70`):** 관측 항목에 선택형 `activation`(`added_check_calls`, `fallback_calls`)을 기록하고 `--check-activation`을 주면, 실행 완료(`clear`/`flag`)된 후보군 사례에서 `added_check`가 실제로 발동했는지(`silent_noop` 차단)와 기준군에서 0회 누출됐는지(`control_leakage` 차단)를 감사한다. 텔레메트리 누락·미발동·누출 시 `activation_audit`와 함께 전용 종료 코드 `70`을 반환한다. `python tools/run_paired_example.py --with-activation`으로 예제 영수증에 발동 카운터를 붙여 결정론적으로 검증할 수 있다.

## 분할 간 픽스처 구조 중복·누출 감사 (v0.34.0)

원시 바이트 `fixture_sha256` 고유성 검사는 공백·키 순서만 바꾼 재포맷 복제나 도메인 문자열(예: SKU·태그 이름)과 행 순서만 바꾼 알파-리네이밍 복제를 걸러내지 못한다. v0.34.0부터 `tools/paired_release.py`는 Python 표준 라이브러리만으로 선택형 `--fixtures-dir <DIR>`와 `--check-fixture-leakage` 옵션을 제공한다(기존 v0.30.0–v0.33.0 CLI·출력 100% 하위호환).

- **실패 폐쇄 입력 검증 (종료 코드 `2`):** `--fixtures-dir`의 심볼릭 링크·재분석 지점, 안전하지 않은 `case_id` 경로 탐색, 누락·1 MiB 초과·`plan.json`의 `fixture_sha256` 불일치, 중복 키·`NaN`/`Infinity`·비객체 JSON을 모두 거부한다.
- **2단계 구조 지문 (`fixture_leakage_audit`):**
  1. `canonical_json_sha256`: JSON 타입(`bool`/`int`/`float`/`null`/`str`)과 배열 순서를 유지한 채 공백과 딕셔너리 키 순서에 불변인 정규화 다이제스트.
  2. `alpha_structural_sha256`: 딕셔너리 키, 최상위 모드 문자열(`gold_mode`, `model_mode` 등), 숫자·불리언·`null` 값과 행 간 문자열 동치 그래프를 보존하면서 중첩 도메인 문자열을 알파-리네이밍하고 배열 행 순서를 정규화한 구조 다이제스트.
- **결과 공시와 강제 (`--check-fixture-leakage`):** 보고서의 `fixture_leakage_audit`에는 원본 행 값이나 로컬 경로 없이 다이제스트·사례 ID·노출 구간(`development`/`heldout`/`unknown`)과 상태(`passed`, `cross_split_leakage`, `within_split_duplicate`, `extra_fixture_entries`, `both_failed`, `multiple_failures`)만 기록한다. `--check-fixture-leakage`를 지정하면 `development`↔`heldout` 간 누출, 동일 분할 내 구조 복제, 또는 계획 외 미등록 픽스처 항목이 하나라도 있을 때 종료 코드 `1`로 실패한다. 저장소의 `examples/paired-release/cases` 10건은 두 수준과 구성원 일치 모두 중복·초과 `0`건으로 통과한다.

## 픽스처 디렉터리 정확 구성원 감사 및 `-O` 이식성 자가 감사 (v0.36.0)

v0.36.0부터 [`openai/math`](https://github.com/openai/math)(커밋 `3344da59`, Apache-2.0)의 `check_records.py`(`check_tree_membership`)와 `portability_check.py` 패턴을 Python 표준 라이브러리만으로 선택 반영했다(새 런타임 의존성 0개).

- **픽스처 디렉터리 정확 구성원 감사 (`tools/paired_release.py`, `tools/run_paired_example.py`):** `--fixtures-dir`에 계획된 `{<case_id>.json}` 외의 미등록 파일(`extra_files`), 하위 디렉터리(`extra_directories`), 심볼릭 링크·비정규 항목(`unexpected_entries`)이 남아 있으면 `tree_membership_passed = false`로 공시하고 `--check-fixture-leakage`에서 종료 코드 `1`로 실패한다(`tools/run_paired_example.py`도 `cases/`의 정확 구성원 일치를 검증한다).
- **이전 CWD · 읽기 전용 · `python -O -B` 변조 거부 자가 감사 (`tools/portability_check.py`):** `python -O -B tools/portability_check.py --json`은 무관한 작업 디렉터리에서 오프라인 검증기(`knowledge-evolution`, `run_paired_example.py`, `paired_release.py`, `result_contract`, `report --permitted-assumptions`)를 `-O`(assert 제거) 모드로 실행해 정상 통과, 원본 파일 SHA-256 읽기 전용 불변성, 그리고 1바이트 영수증 변조·미등록 픽스처 삽입·수치 주장 조작·중복 JSON 키·미허용 가정의 실패 폐쇄를 검증한다.

## 다중 검정 Holm-Bonferroni FWER 진단 (v0.37.0)

동일 비교 실행에서 여러 노출 구간(`heldout`, `development`, `unknown`)과 지표(`detections`, `false_positives`)의 McNemar p-값을 동시에 읽으면 가족별 제1종 오류율(FWER)이 팽창한다. v0.37.0부터 `tools/paired_release.py`는 Python 표준 라이브러리만으로 [Holm (1979)](https://www.jstor.org/stable/4615733)의 단계적 하향 보정(`holm_bonferroni_adjust`)을 계산해 최상위 진단 전용 블록 `multiplicity_diagnostics`(`semantic-crosscheck/holm-bonferroni/v1`, `diagnostic_only = true`)에 함께 기록한다.

- **대상과 출력:** 표본 수 `n > 0`인 모든 평가된 `(exposure, detections/false_positives)` 조합에 대해 `exact_mcnemar`와 `midp_mcnemar` 각각의 원시 p-값(`raw_p`), Holm 보정 p-값(`holm_adjusted_p`), `alpha = 0.05` 미만 건수(`raw_under_alpha`, `holm_under_alpha`)를 출력한다.
- **하위호환성:** 기존 `groups`, `pairs`, `--claims`, `--check-markdown`, 종료 코드는 변경하지 않는다.
