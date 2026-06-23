"""
ETF 전송 게이트 회귀 방지 단위 테스트
대상: tiger_etf_simulator.should_poll_auction / decide_auction_send

버그 맥락:
  GAS가 08:24 깨우고 08:30:00에 antc_cnpr=0 → 스크립트가 skip으로 무전송 종료.
  늦은 cron이 2시간 뒤 폴백 발송(08:30이어야 할 게 10:34 도착).
  수정 핵심: 정시 주 실행(auction_primary_attempted=True)은 antc 미확보여도
             반드시 send_fallback_primary 반환 → 절대 skip 되지 않는다.
"""

import unittest

# 모듈 최상단에서 환경변수·파일 접근은 있으나 네트워크 호출은 없음.
# if __name__ == "__main__" 가드(1336번 줄)로 main() 자동 실행 없음 → import 안전.
from tiger_etf_simulator import should_poll_auction, decide_auction_send


# ---------------------------------------------------------------------------
# decide_auction_send — 2^3=8 진리표 전수 검증
# ---------------------------------------------------------------------------
class TestDecideAuctionSend(unittest.TestCase):
    """전송 게이트 판정 순수 함수의 모든 입력 조합을 검증한다."""

    # --- 우선순위 ① expected_open_valid=True → 항상 send_real ---

    def test_send_real_all_flags_true(self):
        """유효 antc + 나머지 플래그 모두 True → send_real."""
        self.assertEqual(
            decide_auction_send(
                expected_open_valid=True,
                auction_primary_attempted=True,
                after_auction_window=True,
            ),
            "send_real",
        )

    def test_send_real_only_valid_flag(self):
        """유효 antc + 나머지 플래그 모두 False → 여전히 send_real(최우선)."""
        self.assertEqual(
            decide_auction_send(
                expected_open_valid=True,
                auction_primary_attempted=False,
                after_auction_window=False,
            ),
            "send_real",
        )

    def test_send_real_primary_false_window_true(self):
        """유효 antc + primary=False, window=True → send_real."""
        self.assertEqual(
            decide_auction_send(
                expected_open_valid=True,
                auction_primary_attempted=False,
                after_auction_window=True,
            ),
            "send_real",
        )

    def test_send_real_primary_true_window_false(self):
        """유효 antc + primary=True, window=False → send_real."""
        self.assertEqual(
            decide_auction_send(
                expected_open_valid=True,
                auction_primary_attempted=True,
                after_auction_window=False,
            ),
            "send_real",
        )

    # --- 우선순위 ② auction_primary_attempted=True → send_fallback_primary ---
    # 핵심 회귀: 정시 주 실행은 antc 미확보여도 절대 skip 되지 않는다.

    def test_fallback_primary_with_after_window_true(self):
        """[핵심 회귀] valid=False, primary=True, window=True → send_fallback_primary.
        원래 버그: 이 경우가 skip으로 빠져 2시간 뒤 발송됐음."""
        self.assertEqual(
            decide_auction_send(
                expected_open_valid=False,
                auction_primary_attempted=True,
                after_auction_window=True,
            ),
            "send_fallback_primary",
        )

    def test_fallback_primary_with_after_window_false(self):
        """[핵심 회귀] valid=False, primary=True, window=False → send_fallback_primary.
        정시 주 실행이면 after_auction_window 값 무관하게 폴백으로 즉시 발송."""
        self.assertEqual(
            decide_auction_send(
                expected_open_valid=False,
                auction_primary_attempted=True,
                after_auction_window=False,
            ),
            "send_fallback_primary",
        )

    # --- 우선순위 ③ after_auction_window=True → send_fallback_late ---

    def test_fallback_late(self):
        """valid=False, primary=False, window=True → send_fallback_late(뒤늦은 cron)."""
        self.assertEqual(
            decide_auction_send(
                expected_open_valid=False,
                auction_primary_attempted=False,
                after_auction_window=True,
            ),
            "send_fallback_late",
        )

    # --- 우선순위 ④ 모두 False → skip ---

    def test_skip_all_false(self):
        """valid=False, primary=False, window=False → skip(08:30 전 조기 실행만)."""
        self.assertEqual(
            decide_auction_send(
                expected_open_valid=False,
                auction_primary_attempted=False,
                after_auction_window=False,
            ),
            "skip",
        )


# ---------------------------------------------------------------------------
# should_poll_auction — 폴링 진입 조건 검증
# ---------------------------------------------------------------------------
class TestShouldPollAuction(unittest.TestCase):
    """동시호가 폴링 진입 여부 순수 함수를 검증한다."""

    def test_normal_entry_antc_none(self):
        """정상 폴링 진입: 모든 조건 충족, antc=None(아직 미조회) → True."""
        self.assertTrue(
            should_poll_auction(
                auction_only=True,
                no_telegram=False,
                in_preopen_auction=True,
                antc=None,
            )
        )

    def test_antc_cnpr_zero_should_poll(self):
        """antc_cnpr=0(아직 0원) → 아직 유효 체결가 없으므로 폴링 계속 → True."""
        self.assertTrue(
            should_poll_auction(
                auction_only=True,
                no_telegram=False,
                in_preopen_auction=True,
                antc={"antc_cnpr": 0},
            )
        )

    def test_antc_cnpr_positive_should_not_poll(self):
        """antc_cnpr=10300(유효 체결가 확보) → 폴링 불필요 → False."""
        self.assertFalse(
            should_poll_auction(
                auction_only=True,
                no_telegram=False,
                in_preopen_auction=True,
                antc={"antc_cnpr": 10300},
            )
        )

    def test_no_telegram_blocks_poll(self):
        """--no-telegram 플래그 → 전송 자체 안 하므로 폴링도 불필요 → False."""
        self.assertFalse(
            should_poll_auction(
                auction_only=True,
                no_telegram=True,
                in_preopen_auction=True,
                antc=None,
            )
        )

    def test_auction_only_false_blocks_poll(self):
        """--auction-only 없음 → 동시호가 전용 모드 아님 → False."""
        self.assertFalse(
            should_poll_auction(
                auction_only=False,
                no_telegram=False,
                in_preopen_auction=True,
                antc=None,
            )
        )

    def test_not_in_preopen_auction_blocks_poll(self):
        """동시호가 시간대 아님(08:30 이전 또는 09:00 이후) → False."""
        self.assertFalse(
            should_poll_auction(
                auction_only=True,
                no_telegram=False,
                in_preopen_auction=False,
                antc=None,
            )
        )

    def test_antc_missing_key_should_poll(self):
        """antc dict이지만 antc_cnpr 키 없음 → .get('antc_cnpr', 0)=0 → True."""
        self.assertTrue(
            should_poll_auction(
                auction_only=True,
                no_telegram=False,
                in_preopen_auction=True,
                antc={},
            )
        )

    def test_antc_cnpr_negative_should_poll(self):
        """antc_cnpr 음수(이상값) → 0 이하이므로 폴링 진입 → True."""
        self.assertTrue(
            should_poll_auction(
                auction_only=True,
                no_telegram=False,
                in_preopen_auction=True,
                antc={"antc_cnpr": -1},
            )
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
