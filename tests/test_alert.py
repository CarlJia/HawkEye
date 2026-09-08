"""alert 模块测试：达阈值告警、送达后抑制、发送失败可重试、成功复位。"""

from __future__ import annotations

from hawkeye.alert import FailureState


def test_alerts_at_threshold_then_suppresses_after_marked() -> None:
    s = FailureState()
    assert s.record_failure(3) is False
    assert s.record_failure(3) is False
    assert s.record_failure(3) is True  # 达到阈值，应发告警
    s.mark_alerted()  # 告警成功送达
    assert s.record_failure(3) is False  # 之后抑制
    assert s.record_failure(3) is False


def test_retries_until_alert_marked() -> None:
    # 未 mark_alerted（发送失败）时，达阈值后每轮都应返回 True 以便重试
    s = FailureState()
    s.record_failure(2)
    assert s.record_failure(2) is True
    assert s.record_failure(2) is True
    s.mark_alerted()
    assert s.record_failure(2) is False


def test_success_resets_and_can_alert_again() -> None:
    s = FailureState()
    s.record_failure(2)
    assert s.record_failure(2) is True
    s.mark_alerted()
    s.record_success()
    assert s.consecutive_failures == 0
    assert s.alerted is False
    assert s.record_failure(2) is False
    assert s.record_failure(2) is True


def test_threshold_one_alerts_immediately() -> None:
    s = FailureState()
    assert s.record_failure(1) is True
    s.mark_alerted()
    assert s.record_failure(1) is False
