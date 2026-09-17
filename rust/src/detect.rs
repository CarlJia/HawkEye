//! 变更检测（纯函数）。

use std::collections::{HashMap, HashSet};

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DetectResult {
    /// 首次观测，仅建立基线，不通知。
    Baseline { value: String },
    /// 与上次相同。
    Unchanged { value: String },
    /// 发生变更，需通知。
    Changed { old: String, new: String },
}

/// 比较上次值与当前值。previous 为 None 表示尚无记录。
pub fn detect(previous: Option<&str>, current: &str) -> DetectResult {
    match previous {
        None => DetectResult::Baseline {
            value: current.to_string(),
        },
        Some(prev) if prev == current => DetectResult::Unchanged {
            value: current.to_string(),
        },
        Some(prev) => DetectResult::Changed {
            old: prev.to_string(),
            new: current.to_string(),
        },
    }
}

// ---- 列表新条目判定 ----

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DetectNewResult {
    /// 首次观测某列表监控目标：携带当前全部去重 ID，供静默建基线、不通知。
    SeenBaseline { ids: Vec<String> },
    /// 稳态：携带本轮相对已见集合的全部新 ID（保持列表顺序、去重）。
    NewItems { new_ids: Vec<String> },
}

/// 对照已见集合算出新帖 ID。
///
/// previous_seen 为 None → 首次运行，返回 SeenBaseline（当前全部 ID 去重、保序），
/// 供上层静默记入、不推送；否则返回 NewItems，只含不在已见集合中的 ID。
pub fn detect_new(
    previous_seen: Option<&HashSet<String>>,
    current_ids: &[String],
) -> DetectNewResult {
    // 去重且保序。
    let mut deduped: Vec<String> = Vec::new();
    let mut seen_order: HashMap<&str, ()> = HashMap::new();
    for id in current_ids {
        if seen_order.insert(id.as_str(), ()).is_none() {
            deduped.push(id.clone());
        }
    }
    match previous_seen {
        None => DetectNewResult::SeenBaseline { ids: deduped },
        Some(prev) => DetectNewResult::NewItems {
            new_ids: deduped
                .into_iter()
                .filter(|pid| !prev.contains(pid))
                .collect(),
        },
    }
}

/// 已见集合的条数上限，超出后淘汰最久未见的 ID。
pub const SEEN_IDS_MAX: usize = 1000;

/// 并入本轮确认的新 ID，并把已见集合压到 limit 条以内。
///
/// 集合按「最久未见 → 最近见过」排序，超限时从队首淘汰。本轮仍出现在列表页上的
/// ID 会被刷新到队尾：置顶帖长期挂在页面上，若纯按插入顺序淘汰，它迟早被挤出集合
/// 并在下轮被当成新帖再推一次。
pub fn merge_seen(
    prior: &[String],
    current_ids: &[String],
    confirmed: &[String],
    limit: usize,
) -> Vec<String> {
    let mut order: Vec<String> = Vec::new();
    let mut index: HashMap<String, usize> = HashMap::new();
    let push = |v: String, order: &mut Vec<String>, index: &mut HashMap<String, usize>| {
        if let Some(&i) = index.get(&v) {
            // 移到队尾：删除后重插。
            order.remove(i);
            for (j, item) in order.iter().enumerate().skip(i) {
                index.insert(item.clone(), j);
            }
        }
        index.insert(v.clone(), order.len());
        order.push(v);
    };

    for id in prior.iter().chain(confirmed.iter()) {
        push(id.clone(), &mut order, &mut index);
    }
    for pid in current_ids {
        if index.contains_key(pid) {
            let v = pid.clone();
            push(v, &mut order, &mut index);
        }
    }

    if order.len() > limit {
        order[order.len() - limit..].to_vec()
    } else {
        order
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_detect() {
        assert_eq!(
            detect(None, "a"),
            DetectResult::Baseline { value: "a".into() }
        );
        assert_eq!(
            detect(Some("a"), "a"),
            DetectResult::Unchanged { value: "a".into() }
        );
        assert_eq!(
            detect(Some("a"), "b"),
            DetectResult::Changed {
                old: "a".into(),
                new: "b".into()
            }
        );
    }

    #[test]
    fn test_detect_new_baseline() {
        let ids = vec!["a".to_string(), "b".to_string(), "a".to_string()];
        match detect_new(None, &ids) {
            DetectNewResult::SeenBaseline { ids } => {
                assert_eq!(ids, vec!["a".to_string(), "b".to_string()]);
            }
            other => panic!("unexpected: {other:?}"),
        }
    }

    #[test]
    fn test_detect_new_steady() {
        let mut seen: HashSet<String> = HashSet::new();
        seen.insert("a".to_string());
        seen.insert("b".to_string());
        let ids = vec!["b".to_string(), "c".to_string()];
        match detect_new(Some(&seen), &ids) {
            DetectNewResult::NewItems { new_ids } => {
                assert_eq!(new_ids, vec!["c".to_string()]);
            }
            other => panic!("unexpected: {other:?}"),
        }
    }

    #[test]
    fn test_detect_new_all_seen() {
        let seen: HashSet<String> = ["a".to_string(), "b".to_string()].into_iter().collect();
        match detect_new(Some(&seen), &["a".to_string(), "b".to_string()]) {
            DetectNewResult::NewItems { new_ids } => assert!(new_ids.is_empty()),
            other => panic!("unexpected: {other:?}"),
        }
    }

    #[test]
    fn test_detect_new_partial_preserves_order() {
        let seen: HashSet<String> = ["a".to_string()].into_iter().collect();
        match detect_new(
            Some(&seen),
            &["c".to_string(), "a".to_string(), "b".to_string()],
        ) {
            DetectNewResult::NewItems { new_ids } => {
                assert_eq!(new_ids, vec!["c".to_string(), "b".to_string()]);
            }
            other => panic!("unexpected: {other:?}"),
        }
    }

    #[test]
    fn test_detect_new_dedups_current() {
        let seen: HashSet<String> = ["x".to_string()].into_iter().collect();
        match detect_new(
            Some(&seen),
            &["a".to_string(), "a".to_string(), "a".to_string()],
        ) {
            DetectNewResult::NewItems { new_ids } => assert_eq!(new_ids, vec!["a".to_string()]),
            other => panic!("unexpected: {other:?}"),
        }
    }

    #[test]
    fn test_detect_new_empty_current_not_baseline() {
        // 稳态下空列表不是首次运行：返回空的 NewItems，交由调度层归入失败路径。
        let seen: HashSet<String> = ["a".to_string()].into_iter().collect();
        match detect_new(Some(&seen), &[]) {
            DetectNewResult::NewItems { new_ids } => assert!(new_ids.is_empty()),
            other => panic!("unexpected: {other:?}"),
        }
    }

    #[test]
    fn test_merge_seen_under_limit_appends_confirmed() {
        // 未超上限时行为与「旧集合 + 本轮确认」拼接一致
        let merged = merge_seen(&["1".into(), "2".into()], &["3".into()], &["3".into()], 10);
        assert_eq!(
            merged,
            vec!["1".to_string(), "2".to_string(), "3".to_string()]
        );
    }

    #[test]
    fn test_merge_seen_trims_oldest_beyond_limit() {
        let merged = merge_seen(
            &["1".into(), "2".into(), "3".into()],
            &["4".into()],
            &["4".into()],
            3,
        );
        assert_eq!(
            merged,
            vec!["2".to_string(), "3".to_string(), "4".to_string()]
        );
    }

    #[test]
    fn test_merge_seen_refreshes_still_visible_ids() {
        // 置顶帖长期挂在列表页上：本轮仍可见的旧 ID 刷新到队尾，裁剪时淘汰的是
        // 真正最久未见的 "1"。
        let merged = merge_seen(
            &["pin".into(), "1".into(), "2".into(), "3".into()],
            &["4".into(), "pin".into()],
            &["4".into()],
            4,
        );
        assert_eq!(
            merged,
            vec![
                "2".to_string(),
                "3".to_string(),
                "4".to_string(),
                "pin".to_string()
            ]
        );
    }

    #[test]
    fn test_merge_seen_excludes_unconfirmed_new_ids() {
        // 通知发送失败的新帖不进入已见集合，留待下轮重试
        let merged = merge_seen(&["1".into()], &["2".into(), "3".into()], &["2".into()], 10);
        assert_eq!(merged, vec!["1".to_string(), "2".to_string()]);
    }

    #[test]
    fn test_merge_seen_dedups() {
        let merged = merge_seen(
            &["1".into(), "2".into()],
            &["1".into(), "2".into()],
            &["1".into()],
            10,
        );
        assert_eq!(merged, vec!["1".to_string(), "2".to_string()]);
    }

    #[test]
    fn test_merge_seen_shrinks_oversized_prior() {
        // 上限引入前遗留的超长集合，下一次落盘即收敛到上限
        let prior: Vec<String> = (0..(SEEN_IDS_MAX + 50)).map(|i| i.to_string()).collect();
        let merged = merge_seen(&prior, &[], &[], SEEN_IDS_MAX);
        assert_eq!(merged.len(), SEEN_IDS_MAX);
        assert_eq!(merged[0], "50");
    }
}
