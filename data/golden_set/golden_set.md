# Semantica 골든셋 — 20개 질문 (초안)
# 작성일: 2026-08-28 | 작성자: seongin@joycity.com
# ⚠️ 중요: 이 파일은 Semantica 인제스천(ingestion) 실행 전에 완성해야 합니다.
#    답변은 반드시 수집된 Notion 페이지 원문을 보고 직접 확인/수정 후 확정하세요.

---

## 사용 방법
- `answer`: 정답 (원문에서 직접 확인한 내용으로 채워주세요)
- `source_page`: 답변 근거가 된 Notion 페이지 URL
- `category`: 질문 유형
- `difficulty`: easy / medium / hard
- `note`: 검토 시 추가 메모

---

## 카테고리 1: 담당자 (5개)

### Q01
```yaml
question: "점검 시작과 서버 오픈 단계는 어느 팀이 담당하나요?"
answer: "운영팀"
source_page: "https://app.notion.com/p/29bea67a568180058608d72739aaf051"
category: 담당자
difficulty: easy
note: "바이너리 점검 / 일반 점검 양쪽 모두 동일하게 운영팀 담당으로 기재됨"
```

### Q02
```yaml
question: "에러코드 198이 발생했을 때 확인을 요청해야 하는 담당자는 누구인가요?"
answer: "정보시스템팀 안제민"
source_page: "https://app.notion.com/p/2e6ea67a568180d4829fddaa65bde996"
category: 담당자
difficulty: easy
note: "빌드 프로세스 문서 — 에러코드 198: 정보시스템팀 안제민님 확인"
```

### Q03
```yaml
question: "RESU 라이브 + PM 이슈 전달의 담당자는 누구누구인가요?"
answer: "김도형, 허현철, 고명수 / 김정빈, 신동화"
source_page: "https://app.notion.com/p/29bea67a568180058608d72739aaf051"
category: 담당자
difficulty: medium
note: "바이너리 점검 체크리스트 기준. 파일 03에는 신동화, 고명수 순서로 한 명 더 있음 — 정확한 최신 담당자 원문 재확인 필요"
```

### Q04
```yaml
question: "iOS 빌드 관련 채널이 없을 때 데브옵스팀에서 문의할 수 있는 담당자는 누구인가요?"
answer: "임재욱"
source_page: "https://app.notion.com/p/2e6ea67a568180d4829fddaa65bde996"
category: 담당자
difficulty: medium
note: "빌드 프로세스 > Mirror 버그픽스 > 참고 자료 섹션"
```

### Q05
```yaml
question: "애플 앱스토어 iOS 내부테스터를 등록할 때 애니플렉스 측에 요청을 전달하는 담당자는 누구인가요?"
answer: "김원태"
source_page: "https://app.notion.com/p/Live-3c3ea67a568180709d04e9af2e5ef088"
category: 담당자
difficulty: medium
note: "Live 버그픽스 프로세스 > 구글 내부테스트 업로드 섹션"
```

---

## 카테고리 2: 정책/규정 (4개)

### Q06
```yaml
question: "점검 소요 시간 확인은 점검 당일 기준 언제까지 완료해야 하나요?"
answer: "점검 전날 15시까지"
source_page: "https://app.notion.com/p/29bea67a568180058608d72739aaf051"
category: 정책/규정
difficulty: easy
note: "바이너리 점검 / 일반 점검 양쪽 공통 규정"
```

### Q07
```yaml
question: "iOS 버전 표기에서 괄호 안의 숫자(예: 1.9.1(10)에서 10)는 무엇을 의미하나요?"
answer: "번들버전(Bundle Version)"
source_page: "https://app.notion.com/p/Live-3c3ea67a568180709d04e9af2e5ef088"
category: 정책/규정
difficulty: medium
note: "Live 버그픽스 프로세스 > iOS 버전 관리 시트 참고 자료 섹션"
```

### Q08
```yaml
question: "QA 빌드 후 접속까지 소요되는 시간은 얼마로 안내하나요?"
answer: "약 30분"
source_page: "https://app.notion.com/p/2e6ea67a568180d4829fddaa65bde996"
category: 정책/규정
difficulty: easy
note: "빌드 프로세스 > QA 빌드 > 빌드 배포 공유 양식 내 명시"
```

### Q09
```yaml
question: "QA 빌드 공유 시 공유해야 하는 빌드 항목은 어떻게 구성되나요?"
answer: "안드로이드 링크 2개(애니플렉스, 조이시티) + iOS 버전 2개(애니플렉스, 조이시티)를 QA방(+DQA방)에 공유"
source_page: "https://app.notion.com/p/2e6ea67a568180d4829fddaa65bde996"
category: 정책/규정
difficulty: medium
note: "빌드 프로세스 > QA 빌드 > 4. 빌드의 배포 섹션"
```

---

## 카테고리 3: 관계 (5개)

### Q10
```yaml
question: "FDE1팀과 FDE2팀의 기반 조직과 담당 리더는 각각 누구인가요?"
answer: "FDE1팀: 데이터사이언스실 기반, 리더 정민호 / FDE2팀: 플랫폼실 기반, 리더 김주철"
source_page: "https://app.notion.com/p/FDE-26Y2H-3b1ea67a56818123a051d44179731020"
category: 관계
difficulty: medium
note: "FDE 프로세스 도입 안내 세미나 문서 > 3. FDE란 무엇인가 섹션"
```

### Q11
```yaml
question: "온톨로지, 디지털 트윈, End-to-End 도구는 각각 어떤 역할로 설명되나요?"
answer: "온톨로지(규칙) → 디지털 트윈(엔진) → End-to-End 도구(화면)"
source_page: "https://app.notion.com/p/FDE-26Y2H-3b1ea67a56818123a051d44179731020"
category: 관계
difficulty: medium
note: "FDE 문서 > TAKE 섹션 내 '쌓인 지식은 무엇이 되는가'"
```

### Q12
```yaml
question: "FDE 활동 기여도는 무엇에 반영되나요?"
answer: "인사 평가 (GIVE + TAKE 두 기준으로 반영)"
source_page: "https://app.notion.com/p/FDE-26Y2H-3b1ea67a56818123a051d44179731020"
category: 관계
difficulty: easy
note: "FDE 문서 > 8. 일정 섹션 주변"
```

### Q13
```yaml
question: "FDE 파견이 종료되면 도구와 지식은 각각 어디에 남나요?"
answer: "도구는 해당 팀에, 지식(온톨로지)은 전사 온톨로지에 남음"
source_page: "https://app.notion.com/p/FDE-26Y2H-3b1ea67a56818123a051d44179731020"
category: 관계
difficulty: medium
note: "FDE 문서 > FAQ 섹션 — 파견 종료 관련 질문"
```

### Q14
```yaml
question: "AppGuard Upload/Download Timeout 에러가 지속 발생할 경우 어떻게 해야 하나요?"
answer: "시간을 두고 재실행하고, 지속 발생 시 라이브팀에 공유"
source_page: "https://app.notion.com/p/2e6ea67a568180d4829fddaa65bde996"
category: 관계
difficulty: medium
note: "빌드 프로세스 > 빌드 에러 대응 방법 섹션"
```

---

## 카테고리 4: 문서 위치 (3개)

### Q15
```yaml
question: "iOS 버전 관리 시트는 어디서 확인할 수 있나요?"
answer: "https://www.notion.so/joycity/2e6ea67a5681804997f6e69195b4c008"
source_page: "https://app.notion.com/p/2e6ea67a568180d4829fddaa65bde996"
category: 문서위치
difficulty: easy
note: "빌드 프로세스 > QA 빌드 > 3. iOS 빌드 섹션에 링크 명시"
```

### Q16
```yaml
question: "버전 표기 규칙 문서의 파일명은 무엇인가요?"
answer: "STRAT-버전 표기 규칙-101125-144421.pdf"
source_page: "https://app.notion.com/p/Live-3c3ea67a568180709d04e9af2e5ef088"
category: 문서위치
difficulty: medium
note: "Live 버그픽스 프로세스 > 6. 참고 자료 섹션"
```

### Q17
```yaml
question: "pLTV D3D5 관련 도커 이미지는 어느 GCP 레포지토리 경로에 업로드되나요?"
answer: "https://console.cloud.google.com/artifacts/docker/data-science-division-216308/us-west1/pltv-preprocessor-repo/pltv-uid-d3d5-model"
source_page: "https://app.notion.com/p/pLTV-262ea67a568180318081e0995aeb73e6"
category: 문서위치
difficulty: hard
note: "pLTV 페이지 최상단 URL 직접 명시"
```

---

## 카테고리 5: 복합 (3개)

### Q18
```yaml
question: "빌드 중 에러코드 138과 198이 발생했을 때 각각의 대응 방법은 무엇인가요?"
answer: "에러코드 138: 재빌드 / 에러코드 198: 정보시스템팀 안제민 확인 (급한 경우 빌드머신 재부팅 또는 sudo pkill -f Unity.Licensing.Client)"
source_page: "https://app.notion.com/p/2e6ea67a568180d4829fddaa65bde996"
category: 복합
difficulty: hard
note: "빌드 프로세스 > 빌드 에러 대응 > 빌드머신 메모리 부족으로 인한 유니티 크래시 항목"
```

### Q19
```yaml
question: "점검 진행 중 서버 상태 확인에 사용하는 도구는 무엇이 있나요?"
answer: "Grafana, Kibana, OpenSearch 대시보드 (서버 상태 확인 단계에서 사용)"
source_page: "https://app.notion.com/p/29bea67a568180058608d72739aaf051"
category: 복합
difficulty: medium
note: "바이너리 점검 체크리스트 > 서버 종료 확인 / 서버 상태 확인 항목에 링크 포함됨"
```

### Q20
```yaml
question: "IN-JOY가 '왜 매출이 떨어졌나'에 답하지 못하는 이유는 무엇이고, FDE는 이를 어떻게 해결하려 하나요?"
answer: "IN-JOY는 End-to-End 도구(화면)만 먼저 만들었으나 온톨로지(규칙)와 디지털 트윈(엔진)이 비어 있어 분석 불가. FDE의 TAKE 모델로 파견 중 수집한 업무 지식을 온톨로지에 축적하여 AI 분석 기반을 구축하는 것이 해결 방향."
source_page: "https://app.notion.com/p/FDE-26Y2H-3b1ea67a56818123a051d44179731020"
category: 복합
difficulty: hard
note: "FDE 세미나 문서 > TAKE 섹션 > '쌓인 지식은 무엇이 되는가' 참조"
```

---

## 검수 체크리스트 (인제스천 전 확인)

- [ ] 20개 질문 모두 `answer` 필드 원문 확인 완료
- [ ] 각 `source_page` URL이 실제 접근 가능한지 확인
- [ ] 카테고리 분포 확인: 담당자(5) / 정책(4) / 관계(5) / 문서위치(3) / 복합(3)
- [ ] 담당자 이름 오탈자 없음 (김도형, 허현철, 고명수, 김정빈, 신동화, 안제민, 임재욱, 정민호, 김주철, 김원태)
- [ ] Notion 인제스천 실행 전 이 파일을 최종 저장

## 점수 기준 (Semantica 검색 후 평가 시)
- 완전 일치: 1.0점
- 부분 일치 (핵심 정보 포함): 0.5점
- 불일치: 0점
- 목표: 20문항 평균 0.7점 이상 (14점/20점)
