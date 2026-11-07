---
description: myAISky 서버 상태를 점검하고, 장애가 감지되면 weather-healer로 원인 분석·최소 수정을 진행한다.
argument-hint: [증상 설명, 선택]
allowed-tools: Read, Grep, Glob, Bash, Agent
---

myAISky 장애 대응 절차를 실행한다. 사용자가 준 증상: $ARGUMENTS

## 1단계: 현재 상태 확인

`ops-monitor` 에이전트에게 상태 점검을 맡긴다. 점검 항목은 서버 생존, 데이터 소스(`kma`/`mock`), 신선도, 최근 WARN/ERROR, `weather_logs` 적재 추이다.

- 서버가 응답하지 않으면 `backend/` 기동 명령을 사용자에게 안내한다.
  `uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000` (프로젝트 루트에서 실행)
  서버를 직접 띄우지는 않는다.

## 2단계: 판정에 따른 분기

- **정상**: 점검 요약만 보고하고 끝낸다. 수정은 하지 않는다.
- **주의 또는 장애**: `weather-healer` 에이전트를 호출한다. 전달할 내용은 다음과 같다.
  - 1단계 점검 결과 전문
  - 사용자가 준 증상(`$ARGUMENTS`가 있을 때)
  - 제약: 최소 수정만, `.env`와 `weather_logs.db` 수정 금지, 커밋·푸시 금지

## 3단계: 검증

`weather-healer`가 수정을 마쳤다면 `ops-monitor`로 다시 점검한다. 그래도 `source`가 `kma`로 돌아오지 않으면 "수정 완료"라고 쓰지 않는다. 원인과 남은 상태를 그대로 보고한다.

## 보고

- 점검 결과(전/후 비교)
- 원인과 수정 내용(파일:라인)
- 검증 결과
- 사용자가 직접 해야 할 일(예: `KMA_API_KEY` 설정, 서버 재기동)

변경된 파일은 커밋하지 않는다. 커밋이 필요하면 사용자에게 `deploy-guardian` 검토를 거쳐 진행할지 묻는다.
