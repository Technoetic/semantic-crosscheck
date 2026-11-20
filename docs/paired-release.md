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
