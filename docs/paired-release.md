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
