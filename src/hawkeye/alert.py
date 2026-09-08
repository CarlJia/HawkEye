"""失败告警状态机（进程内，重启即重置）。

边沿触发：连续失败达到阈值后发一条告警（在成功送达前每轮重试），送达后
抑制，直到一次成功抓取复位。v1 不发送“恢复”通知。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class FailureState:
    """单个目标的连续失败计数与告警抑制标志。"""

    consecutive_failures: int = 0
    alerted: bool = False

    def record_failure(self, threshold: int) -> bool:
        """记一次失败；已达阈值且尚未成功告警时返回 True（应尝试发告警，可跨轮重试）。"""
        self.consecutive_failures += 1
        return self.consecutive_failures >= threshold and not self.alerted

    def mark_alerted(self) -> None:
        """告警成功送达后调用，抑制后续重复告警，直到一次成功抓取复位。"""
        self.alerted = True

    def record_success(self) -> None:
        """成功即复位计数与告警抑制。"""
        self.consecutive_failures = 0
        self.alerted = False
