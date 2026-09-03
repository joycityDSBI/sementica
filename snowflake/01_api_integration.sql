-- ================================================================
-- Semantica × Snowflake — Step 2+3: API Integration 생성
-- ================================================================
-- 실행 전 확인 사항:
--   1. ngrok가 서버에서 실행 중이어야 합니다.
--   2. NGROK_URL 자리에 실제 ngrok 주소를 입력하세요.
--      예) https://a1b2c3d4.ngrok-free.app
--   3. ACCOUNTADMIN 권한이 필요합니다.
--
-- ngrok 실행 방법 (서버에서):
--   ngrok http 8766
--   → Forwarding: https://xxxx.ngrok-free.app → http://localhost:8766
-- ================================================================

USE ROLE ACCOUNTADMIN;

-- ── API Integration 생성 ─────────────────────────────────────────
-- API_PROVIDER = generic_public : AWS/Azure/GCP가 아닌 일반 HTTPS 엔드포인트
-- NGROK_URL 자리에 실제 ngrok 주소 입력 (슬래시로 끝내야 함)
CREATE OR REPLACE API INTEGRATION semantica_api_integration
  API_PROVIDER        = generic_public
  API_ALLOWED_PREFIXES = ('https://agility-unadvised-constrain.ngrok-free.dev/')
  ENABLED             = TRUE;

-- 생성 확인
DESC INTEGRATION semantica_api_integration;
