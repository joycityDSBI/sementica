---
title: Live 버그픽스 프로세스
notion_url: https://app.notion.com/p/Live-3c3ea67a568180709d04e9af2e5ef088
page_id: 3c3ea67a568180709d04e9af2e5ef088
---


---
> 이 문서는 버그픽스를 진행하기 위한 내용을 포함합니다.
# 1. 리비전 머지
- RESU BTS 의 권장 바이너리 탭에서 진행할 버그픽스의 리비전을 라이브로 머지합니다.

# 2. 버전 변경

# 3. 구글 버그픽스 빌드 (AOS)
- 해당 빌드는 클라이언트 코드 수정에 의한 빌드입니다. 
- 리소스가 수정된 내역이 있다면 리소스 빌드도 필요합니다. 
- (팀시티) Client_Live> Live_Google > Bugfix > Run 
- (팀시티) Client_Live> Live_GoogleJoy > Bugfix > Run 
- RESU 빌드 알림 채널에 알림을 확인해주세요.
- 빌드가 완료되면 다운로드 받아서 접속 테스트를 합니다.
# 4. 애플 버그픽스 빌드 (iOS)
- 해당 빌드는 클라이언트 코드 수정에 의한 빌드입니다. 
- 리소스가 수정된 내역이 있다면 리소스 빌드도 필요합니다. 
- 버전 변경 주의 사항
- (팀시티) Client_Live> Live_Apple > Bugfix > Run 옆에 커스텀 Run 을 누릅니다.
- (팀시티) Client_Live> Live_AppleJoy > Bugfix > Run 옆에 커스텀 Run 을 누릅니다.
- RESU 빌드 알림 채널에 알림을 확인해주세요.
- 빌드가 완료되면 테스트 플라이트에 자동으로 등록됩니다.

# 4. 구글 내부테스트 업로드
- Artifacts 다운로드

- 내부 테스트
- 
# 6. 참고 자료
- 버전 표기 규칙 
- STRAT-버전 표기 규칙-101125-144421.pdf
  - (팀시티) Client_Live > Live_Google > Resource > Pending 리비전 중 리소스가 있는지 확인
  - (팀시티) Client_Live > Live_GoogleJoy > Resource > Pending 리비전 중 리소스가 있는지 확인
  - 빌드 에러 발생 시 대응 방법 
  - (팀시티) Client_Live > Live_Apple > Resource > Pending 리비전 중 리소스가 있는지 확인
  - (팀시티) Client_Live > Live_AppleJoy > Resource > Pending 리비전 중 리소스가 있는지 확인
  - iOS 는 테스트 플라이트에 등록을 하기 때문에 버전을 체크해야합니다. 아래 버전 관리 시트로 확인해주시면 됩니다.
  - iOS 버전 관리 시트 
    - 애니플렉스 / 조이시티 버전 별개로 동작합니다. 조이시티도 빌드가 필요하다면 같은 버전으로 맞춰주는게 좋습니다.
    - 1.9.1 (10) 에서 1.9.1은 버전, (10)은 번들버전으로 명명합니다.
  - Parameter 탭을 선택 > Bundle Version 을 입력합니다.
  - Run Build 
  - Parameter 탭을 선택 > Bundle Version 을 입력합니다.
  - Run Build 
  - 등록까지 시간이 소요되며 테스트 플라이트를 설치해두셨다면 설치 가능 시점에 자동으로 알림이 옵니다.
  - 아래 리스트의 인원들만 자동으로 등록됩니다. (애니플렉스)
    - 내부테스터로 등록하려면 김원태님을 통해 애니플렉스 측에 요청해주시면 됩니다.
    - 이후 아래 그룹에 추가해주시면 됩니다.