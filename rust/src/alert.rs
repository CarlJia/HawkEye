//! 失败告警状态机（进程内，重启即重置）。
//!
//! 边沿触发：连续失败达到阈值后发一条告警（在成功送达前每轮重试），送达后
//! 抑制，直到一次成功抓取复位。

use std::collections::HashMap;

#[derive(Debug, Clone, Default, PartialEq)]
pub struct FailureState {
    pub consecutive_failures: i64,
    pub alerted: bool,
}

impl FailureState {
    /// 记一次失败；已达阈值且尚未成功告警时返回 true（应尝试发告警，可跨轮重试）。
    pub fn record_failure(&mut self, threshold: i64) -> bool {
        self.consecutive_failures += 1;
        self.consecutive_failures >= threshold && !self.alerted
    }

    /// 告警成功送达后调用，抑制后续重复告警，直到一次成功抓取复位。
    pub fn mark_alerted(&mut self) {
        self.alerted = true;
    }

    /// 成功即复位计数与告警抑制。
    pub fn record_success(&mut self) {
        self.consecutive_failures = 0;
        self.alerted = false;
    }
}

/// 各目标失败状态的登记表（页面级 / 元素级 / watch 级共用）。
#[derive(Debug, Default)]
pub struct FailureTracker {
    states: HashMap<String, FailureState>,
}

impl FailureTracker {
    pub fn get_mut(&mut self, identity: &str) -> &mut FailureState {
        self.states.entry(identity.to_string()).or_default()
    }

    pub fn get(&self, identity: &str) -> Option<&FailureState> {
        self.states.get(identity)
    }

    pub fn contains_key(&self, identity: &str) -> bool {
        self.states.contains_key(identity)
    }

    pub fn remove(&mut self, identity: &str) {
        self.states.remove(identity);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_edge_triggered() {
        let mut f = FailureState::default();
        assert!(!f.record_failure(3));
        assert!(!f.record_failure(3));
        assert!(f.record_failure(3));
        f.mark_alerted();
        // 已告警后不再重复告警。
        assert!(!f.record_failure(3));
        f.record_success();
        assert!(!f.record_failure(3));
        assert!(!f.record_failure(3));
        assert!(f.record_failure(3));
    }

    #[test]
    fn test_threshold_one() {
        let mut f = FailureState::default();
        assert!(f.record_failure(1));
    }

    #[test]
    fn test_retries_until_alert_marked() {
        // 未成功告警前，每轮都应继续尝试。
        let mut f = FailureState::default();
        assert!(!f.record_failure(3));
        assert!(!f.record_failure(3));
        assert!(f.record_failure(3));
        // 尝试发送但失败（未 mark_alerted）→ 下一轮仍返回 true 重试。
        assert!(f.record_failure(3));
    }

    #[test]
    fn test_success_resets_and_can_alert_again() {
        let mut f = FailureState::default();
        f.record_failure(3);
        f.mark_alerted();
        f.record_success();
        assert!(!f.record_failure(3));
        assert!(!f.record_failure(3));
        assert!(f.record_failure(3));
    }
}
