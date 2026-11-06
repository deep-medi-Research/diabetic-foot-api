# 변경 기록

분석 API의 버전별 변경이고 최신 버전이 위입니다.
형식은 [Keep a Changelog 1.1.0](https://keepachangelog.com/ko/1.1.0/)을 따르고, 버전은 [유의적 버전](https://semver.org/lang/ko/)입니다. `API_VERSION` 파일과 git 태그 `v<버전>`이 같고, 올리는 기준은 [`docs/analysis-api.md`](docs/analysis-api.md) 10절에 있습니다.

쓰는 규칙은 아래와 같습니다.

- 태그를 달기 전의 변경은 `## [Unreleased]`에 모으고, 태그를 달 때 그 제목을 `## [<버전>] - <YYYY-MM-DD>`로 바꾸고 `API_VERSION`을 같은 번호로 고칩니다.
- 분류는 `### 추가`, `### 변경`, `### 폐기 예정`, `### 제거`, `### 수정`, `### 보안` 순서이고 항목이 없는 분류는 쓰지 않습니다.
- 항목은 한 줄 목록이고, 호환이 깨지는 항목(MAJOR)은 `(호환 깨짐)`으로 시작해 분류의 맨 위에 둡니다.
- README, 그림, CI만 바꾸면 버전을 올리지 않고 여기에 적지 않습니다.

## [Unreleased]

## [1.0.0] - 2026-11-06

### 추가

- 클라이언트 앱과 분석 프로그램이 주고받는 방식의 첫 버전입니다. 명령줄, 요청 줄(분석, preload, presegment), `[dfr]` 출력 줄, 결과 파일, 오류 규칙을 `docs/analysis-api.md`에 적었습니다.
- 요청 줄, `[dfr]` 줄, 요약, `VARIANT.json`의 JSON Schema(draft 2020-12)를 `schema/`에 두었습니다.
- 분석 프로그램은 준비 줄과 요약 `dfr`에 `api_version`과 `worker_version`을 적고, 미리 읽기를 켜면 `dfr.prestream.enabled`를 적습니다. 클라이언트 앱은 `api_version`의 주 버전만 견줍니다.
- 분석 프로그램의 API 위반을 찾는 확인 도구 `conformance/check_analyzer.py`와 API대로 답하는 가짜 분석 프로그램 `fake/fake_analyzer.py`를 더했습니다.
