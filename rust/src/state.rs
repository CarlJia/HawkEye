//! 状态持久化。
//!
//! 扁平 JSON，单文件内支持两种条目形状：元素文本条目与列表已见集合条目。
//! 写入采用先写 `.tmp` 再原子替换；读取到损坏文件时备份并从空状态重建。

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use chrono::Local;
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum Entry {
    /// 单个元素目标的最近已知值。
    State { value: String, updated_at: String },
    /// 单个列表监控目标的已见帖子 ID 集合（保序去重存储）。
    SeenSet {
        seen_ids: Vec<String>,
        updated_at: String,
    },
}

/// 本地时区、精确到秒的 ISO8601 时间戳。
pub fn now_iso() -> String {
    Local::now().format("%Y-%m-%dT%H:%M:%S%:z").to_string()
}

pub type State = HashMap<String, Entry>;

/// 读取状态文件；不存在返回空；损坏则备份后返回空。
pub fn load_state(path: &Path) -> State {
    if !path.exists() {
        return State::new();
    }
    match std::fs::read_to_string(path) {
        Ok(text) => match serde_json::from_str::<State>(&text) {
            Ok(state) => state,
            Err(e) => {
                backup_corrupt(path, &e.to_string());
                State::new()
            }
        },
        Err(e) => {
            backup_corrupt(path, &e.to_string());
            State::new()
        }
    }
}

fn backup_corrupt(p: &Path, e: &str) {
    let ts = Local::now().format("%Y%m%d%H%M%S");
    let backup = p.with_file_name(format!("{}.corrupt.{ts}", p.file_name().unwrap_or_default().to_string_lossy()));
    let backup: PathBuf = backup;
    match std::fs::rename(p, &backup) {
        Ok(()) => tracing::error!("状态文件损坏（{e}），已备份到 {} 并重置为空", backup.display()),
        Err(be) => tracing::error!("状态文件损坏（{e}），且备份失败：{be}"),
    }
}

/// 原子写入：先写临时文件再原子替换。
pub fn save_state(path: &Path, state: &State) {
    let payload = serde_json::to_string_pretty(state).unwrap_or_default();
    let tmp = path.with_file_name(format!("{}.tmp", path.file_name().unwrap_or_default().to_string_lossy()));
    if let Err(e) = std::fs::write(&tmp, payload) {
        tracing::error!("写状态临时文件失败：{e}");
        return;
    }
    if let Err(e) = std::fs::rename(&tmp, path) {
        tracing::error!("原子替换状态文件失败：{e}");
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_roundtrip() {
        let mut state = State::new();
        state.insert(
            "m / p / e".to_string(),
            Entry::State { value: "有货".into(), updated_at: "t".into() },
        );
        state.insert(
            "watch / w".to_string(),
            Entry::SeenSet { seen_ids: vec!["a".into()], updated_at: "t".into() },
        );
        let tmp = std::env::temp_dir().join("hawkeye_state_test.json");
        save_state(&tmp, &state);
        let loaded = load_state(&tmp);
        assert_eq!(loaded.len(), 2);
        assert_eq!(loaded.get("m / p / e"), state.get("m / p / e"));
        assert_eq!(loaded.get("watch / w"), state.get("watch / w"));
        let _ = std::fs::remove_file(&tmp);
    }

    #[test]
    fn test_corrupt_rebuilds() {
        let tmp = std::env::temp_dir().join("hawkeye_state_corrupt_test.json");
        std::fs::write(&tmp, "{not json").unwrap();
        let state = load_state(&tmp);
        assert!(state.is_empty());
        // 备份文件已生成。
        let mut found = false;
        if let Some(dir) = tmp.parent() {
            if let Ok(entries) = std::fs::read_dir(dir) {
                for e in entries.flatten() {
                    if e.file_name().to_string_lossy().starts_with("hawkeye_state_corrupt_test.json.corrupt.") {
                        let _ = std::fs::remove_file(e.path());
                        found = true;
                    }
                }
            }
        }
        assert!(found);
        let _ = std::fs::remove_file(&tmp);
    }

    #[test]
    fn test_atomic_no_tmp_left() {
        let tmp = std::env::temp_dir().join("hawkeye_state_tmp_test.json");
        let mut state = State::new();
        state.insert("k".to_string(), Entry::State { value: "v".into(), updated_at: "t".into() });
        save_state(&tmp, &state);
        assert!(!tmp.with_file_name("hawkeye_state_tmp_test.json.tmp").exists());
        let _ = std::fs::remove_file(&tmp);
    }

    #[test]
    fn test_wrong_shape_treated_as_corrupt() {
        // 根节点不是对象 / 条目字段类型错误都按损坏处理。
        for content in ["[1,2]", "\"str\"", "{\"k\": {\"value\": 1, \"updated_at\": \"t\"}}"] {
            let tmp = std::env::temp_dir().join(format!("hawkeye_state_shape_{}.json", content.len()));
            std::fs::write(&tmp, content).unwrap();
            let state = load_state(&tmp);
            assert!(state.is_empty(), "应按损坏处理：{content}");
            // 清理备份。
            if let Some(dir) = tmp.parent() {
                let prefix = format!("{}.corrupt.", tmp.file_name().unwrap().to_string_lossy());
                if let Ok(entries) = std::fs::read_dir(dir) {
                    for e in entries.flatten() {
                        let name = e.file_name().to_string_lossy().to_string();
                        if name.starts_with(&prefix) {
                            let _ = std::fs::remove_file(e.path());
                        }
                    }
                }
            }
            let _ = std::fs::remove_file(&tmp);
        }
    }

    #[test]
    fn test_seen_set_roundtrip() {
        let mut state = State::new();
        state.insert(
            "watch / w".to_string(),
            Entry::SeenSet { seen_ids: vec!["a".into(), "b".into()], updated_at: "t".into() },
        );
        let tmp = std::env::temp_dir().join("hawkeye_state_seen_test.json");
        save_state(&tmp, &state);
        let loaded = load_state(&tmp);
        assert_eq!(
            loaded.get("watch / w"),
            Some(&Entry::SeenSet { seen_ids: vec!["a".into(), "b".into()], updated_at: "t".into() })
        );
        let _ = std::fs::remove_file(&tmp);
    }

    #[test]
    fn test_seen_set_empty_roundtrip() {
        let mut state = State::new();
        state.insert(
            "watch / w".to_string(),
            Entry::SeenSet { seen_ids: vec![], updated_at: "t".into() },
        );
        let tmp = std::env::temp_dir().join("hawkeye_state_seen_empty_test.json");
        save_state(&tmp, &state);
        let loaded = load_state(&tmp);
        assert_eq!(
            loaded.get("watch / w"),
            Some(&Entry::SeenSet { seen_ids: vec![], updated_at: "t".into() })
        );
        let _ = std::fs::remove_file(&tmp);
    }

    #[test]
    fn test_mixed_shapes_preserved() {
        let mut state = State::new();
        state.insert("e".to_string(), Entry::State { value: "v".into(), updated_at: "t".into() });
        state.insert(
            "watch / w".to_string(),
            Entry::SeenSet { seen_ids: vec!["x".into()], updated_at: "t".into() },
        );
        let tmp = std::env::temp_dir().join("hawkeye_state_mixed_test.json");
        save_state(&tmp, &state);
        let loaded = load_state(&tmp);
        assert_eq!(loaded.len(), 2);
        assert!(matches!(loaded.get("e"), Some(Entry::State { .. })));
        assert!(matches!(loaded.get("watch / w"), Some(Entry::SeenSet { .. })));
        let _ = std::fs::remove_file(&tmp);
    }

    #[test]
    fn test_seen_ids_non_str_treated_as_corrupt() {
        let tmp = std::env::temp_dir().join("hawkeye_state_seen_nonstr_test.json");
        std::fs::write(&tmp, r#"{"k": {"seen_ids": [1], "updated_at": "t"}}"#).unwrap();
        assert!(load_state(&tmp).is_empty());
        if let Some(dir) = tmp.parent() {
            let prefix = format!("{}.corrupt.", tmp.file_name().unwrap().to_string_lossy());
            if let Ok(entries) = std::fs::read_dir(dir) {
                for e in entries.flatten() {
                    if e.file_name().to_string_lossy().starts_with(&prefix) {
                        let _ = std::fs::remove_file(e.path());
                    }
                }
            }
        }
        let _ = std::fs::remove_file(&tmp);
    }

    #[test]
    fn test_seen_ids_not_list_treated_as_corrupt() {
        let tmp = std::env::temp_dir().join("hawkeye_state_seen_notlist_test.json");
        std::fs::write(&tmp, r#"{"k": {"seen_ids": "abc", "updated_at": "t"}}"#).unwrap();
        assert!(load_state(&tmp).is_empty());
        if let Some(dir) = tmp.parent() {
            let prefix = format!("{}.corrupt.", tmp.file_name().unwrap().to_string_lossy());
            if let Ok(entries) = std::fs::read_dir(dir) {
                for e in entries.flatten() {
                    if e.file_name().to_string_lossy().starts_with(&prefix) {
                        let _ = std::fs::remove_file(e.path());
                    }
                }
            }
        }
        let _ = std::fs::remove_file(&tmp);
    }

    #[test]
    fn test_missing_returns_empty() {
        let state = load_state(Path::new("/nonexistent/nowhere.json"));
        assert!(state.is_empty());
    }
}
