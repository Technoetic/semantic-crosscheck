# 검사 ID 목록

이 문서는 README의 '검사 ID 목록' 절을 옮긴 참조 문서입니다(v0.27.0). 정식 목록과 판정 규칙은 [설계 문서 6절](../design.md#6-check-catalogue)입니다.


<details>
<summary><b>단계별 검사 조건 전체 펼치기</b></summary>

| 단계 | 검사 ID | 통과 조건 |
|---|---|---|
| doctor | `python`, `pg8000`, `dsn`, `connect`, `pg_version`, `privileges:temporary` | 필수 환경·PostgreSQL 버전·임시 테이블 권한 충족 |
| doctor | `privileges:select:<entity>`, `privileges:no_write:<entity>`, `output_schema` | 원본 조회 가능, 원본 쓰기·소유·스키마 생성 불가, 출력 스키마 사용 가능 |
| doctor | `reason:*`, `cube:*` | 필요한 구성 요소 준비; 선택 요소 누락은 not_proven으로 보고 |
| demo | `demo:rows:<table>`, `demo:copy_on_error` | 적재 건수가 생성 기준과 일치, 잘못된 행만 COPY에서 제외 |
| build | `shape:<id>` | 위반 없음; warning 규칙은 건수를 보고하고 통과 |
| build | `closure:<h>`, `closure_apply:<h>` | 순환 정책·최대 깊이 충족, 적용한 폐포가 재계산과 일치 |
| build | `rule_view:<id>`, `rule_subjects:view` | 0.22.0부터, `[data_rules]`의 `views = true`일 때만: 현재 규칙 파일로 규칙별 조회 뷰 `v_rule_<id>`와 문제 키 뷰 `v_rules_subjects`를 생성. 뷰의 행이 맞는지는 data-rules가 확인 |
| reason | `shacl:<id>` | pySHACL의 위반 대상과 SQL 위반 키 일치 |
| reason | `shacl:meta:generated` | 0.10.0부터: 생성한 규칙을 shacl-shacl 구문 규칙의 부분집합으로 사전 검사 |
| reason | `shacl:pair:<id>` | 0.11.0부터, `verify.shacl_pairs = true`일 때만: `t.<a> <op> t.<b>` 꼴 `sql_violation`을 SHACL 속성 쌍으로 바꾼 pySHACL 위반 대상과 SQL 위반 키 일치 |
| reason | `owl:consistency`, `v5:closure:<h>`, `v5:defined:<name>` | OWL 일관성, OWL의 폐포·정의 클래스가 SQL과 일치 |
| reason | `v5:witness:<name>` | 선택한 합성 예제에서 HermiT·SQL이 독립 기대 결과와 일치; 실제 데이터의 공허성은 별도 유지 |
| cube | `cube:model`, `cube:meta`, `cube:gold_members:<gold>` | 모델 생성, 공개 멤버 목록 정확(Cube 버전·SQL 플래너 기록), gold 질의 멤버 존재·공개 |
| verify | `v1:<gold>`, `v2:<gold>:<stratum>` | Cube 타입에 맞춘 기준 SQL·Cube 행 일치. 양쪽 모두 0행인 비교도 통과하며 `evidence`의 0행 목록에 남음. 0.23.0부터 `verify.gold_observation = "require"`에서는 모든 비교가 0행인 gold 질의의 통과한 `v1:<gold>`가 `not_run`(`no_rows_compared`) |
| verify | `v3:total:<member>`, `v3:additive:<member>:<member>`, `v3:pair:<member>+<member>` | 합계·그룹 합(NULL 그룹과 독립 SQL 비교 포함)·지표 조합 일치 |
| verify | `v3:closure_oracle:<h>` | 원본 연결의 독립 계산과 저장 폐포의 정확한 수량·논리 경로 수·최단/최장 깊이 일치 |
| verify | `v3w:pair_witness:<member>+<member>` | 0.11.0부터, `verify.existence_witness = true`일 때만: 표본·조각 지표 쌍에서 단독 응답에 빠진 키가 설정의 필터로도 사실 행이 없고 결합 값이 비어 있음(NULL). 단독 응답에 있는 키는 확인하지 않음. 쌍 검사 자체의 판정은 증인이 실패해도 바뀌지 않음 |
| verify | `v4:<operator>:<target>` | 다시 실행한 suite 항목(gold 항목 또는 V3 항목) 하나가 변형 모델에서 실패(`killed_by`). gold 질의가 검출했다는 뜻은 `detection = authored`일 때뿐이며, `generated_only`는 생성 계층끼리의 검사만 실패했다는 뜻. 0.23.0부터 `verify.gold_observation = "require"`에서는 `generated_only`·`unattributed`가 실패(`verify.accepted_gaps`에 없을 때) |
| verify | `gold_evidence:<element>` | 0.23.0부터, `verify.gold_observation = "require"`이고 전체 범위 실행일 때만: 지표·계층·`ontology.class_column`마다 모든 facet이 사람이 쓴 gold 질의와 값이 있는 행으로 비교되었거나, 사람이 쓴 항목의 변이 검출이 있거나, `verify.accepted_gaps`에 있음 |
| report | `stage:<name>` | 단계가 실행되었고 최신이며 통과 |
| build·reason·cube·verify / report | `data_revision:stable`, `data_revision:current` | 구성된 데이터 버전이 안정적이며 현재 근거와 일치; 조회 오류는 실패로 닫음 |
| build·reason·cube·verify / report | `runtime_evidence:stable`, `runtime_evidence:current` | 선택한 실제 실행 환경 근거가 안정적이며 현재와 일치; 누락·변경·조회 실패 시 기존 PASS 재사용 차단 |
| counterexample | `counterexample:search`, `counterexample:query`, `counterexample:recheck`, `counterexample:inputs`, `counterexample:artifact` | 합성 SQL 차이와 축소·재현 범위; 오류·미발견은 동등성 증명 불가, 재생 파일 크기 상한 초과 시 저장 거부 |
| expect | `expect:<gold>[<i>]`, `expect:build`, `expect:cube`, `expect:fixture`, `expect:cases` | fixture에서 gold SQL과 Cube가 모두 기대 행과 일치; 서로 같은데 기대 행과 다르면 `common_mode`(authored)·`regression`(captured) 실패, build 전과 사례 전후에 잰 fixture 테이블 해시가 다르면 통과 불가(그 시점들의 비교일 뿐 한 시점의 같은 데이터를 보장하지는 않음) |
| data-rules | `data_rules:snapshot`, `data_rules:key:<entity>` | 0.22.0부터: 하나의 READ ONLY REPEATABLE READ 스냅샷에서 행 예산·게이트·내보내기를 마침, 노드 키(와 `key`로 보고하는 엔터티의 키)에 NULL·중복 없음 |
| data-rules | `rule:<id>`, `rule_xcheck:<id>`, `rule_example:<id>` | 컴파일한 PostgreSQL과 독립 Python 평가의 결과가 같고 직접 쓴 예제 행과도 같음. `violation` 규칙이 행을 찾으면 실패, `warning` 규칙은 건수를 보고하고 통과. 두 평가가 다르면 `paths_disagree`, 서로 같은데 예제와 다르면 `common_mode`로 규칙은 `not_run` |
| data-rules | `rule_lookup:<id>`, `rule_subjects:lookup` | `v_rule_<id>`·`v_rules_subjects`가 합의된 행을 정해진 순서(`ord`)로 정확히 반환. build가 지금의 규칙 파일로 만들지 않은 뷰는 `view_stale` |

조기 중단의 `stage`, doctor의 `database`, demo의 `demo:load`, verify의 `v4:suite`, 범위를 나타내는 `scope:*`, 설치의 `cube_install:*`까지 포함한 전체 목록은 [설계 문서의 카탈로그](../../docs/design.md#6-check-catalogue)를 참고합니다.

</details>
