//! 元素文本提取（纯粹围绕 CDP 页面的读取与归一化）。
//!
//! Playwright 语义在 chromiumoxide 上的等价实现：CSS 选择器走 querySelector，
//! XPath 走 document.evaluate；「等元素 attached」用带超时的轮询 evaluate 实现。

use std::time::Duration;

use chromiumoxide::Page;

use crate::config::{MonitoredElement, WatchTarget};

/// 去首尾空白，并把内部连续空白（含换行/缩进）折叠为单个空格。
pub fn normalize_text(s: &str) -> String {
    s.split_whitespace().collect::<Vec<_>>().join(" ")
}

/// 元素提取结果：value = 归一化后的字符串（或空串、None）；
/// reason = 失败原因（仅在 value 不可用时非 None）。
#[derive(Debug, Clone, PartialEq)]
pub struct ExtractResult {
    pub value: Option<String>,
    pub reason: Option<String>,
}

/// 等元素挂载（attached）：在超时窗口内以 100ms 间隔轮询，可被提前打断。
async fn wait_attached(page: &Page, selector_js: &str, timeout: Duration) -> bool {
    let deadline = tokio::time::Instant::now() + timeout;
    let expr =
        format!("(()=>{{const els={selector_js};return els&&els.length>0?els[0]:els||null}})()");
    loop {
        let found = page
            .evaluate_expression(expr.clone())
            .await
            .ok()
            .and_then(|r| r.into_value::<serde_json::Value>().ok())
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        if found {
            return true;
        }
        if tokio::time::Instant::now() >= deadline {
            return false;
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
}

/// 等所有匹配挂载后的全部元素数组表达式。
fn all_selector_expr(selector: &str, effective_type: &str) -> String {
    let sel = selector.trim();
    if effective_type == "xpath" {
        let body = sel.strip_prefix("xpath=").unwrap_or(sel);
        format!(
            "(()=>{{const r=document.evaluate({body:?},document,null,XPathResult.ORDERED_NODE_SNAPSHOT_TYPE,null);const out=[];for(let i=0;i<r.snapshotLength;i++)out.push(r.snapshotItem(i));return out}})()"
        )
    } else {
        let body = sel.strip_prefix("css=").unwrap_or(sel);
        format!("Array.from(document.querySelectorAll({body:?}))")
    }
}

/// 谓词表达式：匹配集是否非空（用于首项挂载等待）。
fn any_selector_expr(selector: &str, effective_type: &str) -> String {
    let sel = selector.trim();
    if effective_type == "xpath" {
        let body = sel.strip_prefix("xpath=").unwrap_or(sel);
        format!(
            "(()=>{{const r=document.evaluate({body:?},document,null,XPathResult.ORDERED_NODE_SNAPSHOT_TYPE,null);return r.snapshotLength>0}})()"
        )
    } else {
        let body = sel.strip_prefix("css=").unwrap_or(sel);
        format!("document.querySelector({body:?})!==null")
    }
}

async fn eval_string(page: &Page, expr: &str) -> Option<String> {
    page.evaluate_expression(expr.to_string())
        .await
        .ok()
        .and_then(|r| r.into_value::<serde_json::Value>().ok())
        .and_then(|v| match v {
            serde_json::Value::String(s) => Some(s),
            serde_json::Value::Null => None,
            other => Some(other.to_string()),
        })
}

/// 取匹配元素的状态值，连同失败原因一起返回。
///
/// 文本模式（element.js 缺省）：等元素 attached，取 innerText（空时 textContent 兜底），
/// 经 normalize_text 归一化。JS 模式：先等元素 attached，再对元素求值 JS 表达式。
pub async fn extract_text(
    page: &Page,
    element: &MonitoredElement,
    timeout: Duration,
) -> ExtractResult {
    let idx = element.nth.unwrap_or(0);
    let sel_expr = all_selector_expr(&element.selector, element.effective_selector_type());

    // 等首项挂载（domcontentloaded 后 JS 渲染元素可能还没挂载）。
    if !wait_attached(
        page,
        &any_selector_expr(&element.selector, element.effective_selector_type()),
        timeout,
    )
    .await
    {
        let title = page.get_title().await.ok().flatten().unwrap_or_default();
        let reason = if title.is_empty() {
            "选择器未匹配到元素".to_string()
        } else {
            format!("选择器未匹配到元素（页面标题：{title:?}，可能是反爬挑战页）")
        };
        return ExtractResult {
            value: None,
            reason: Some(reason),
        };
    }

    let nth_expr =
        format!("(()=>{{const els={sel_expr};return els.length>{idx}?els[{idx}]:null}})()");

    if let Some(js) = &element.js {
        // JS 模式：把元素作为参数求值。表达式的 this 是元素；兼容 (el) => ... 形式。
        let wrapped = format!(
            "(()=>{{const el={nth_expr};if(!el)return null;const fn={js};return typeof fn==='function'?fn.call(el,el):fn}})()"
        );
        match page.evaluate_expression(wrapped).await {
            Ok(r) => match r.into_value::<serde_json::Value>() {
                Ok(serde_json::Value::Null) => {
                    return ExtractResult {
                        value: None,
                        reason: Some("JS 表达式未返回值".to_string()),
                    };
                }
                Ok(v) => {
                    let s = match v {
                        serde_json::Value::String(s) => s,
                        other => other.to_string(),
                    };
                    return ExtractResult {
                        value: Some(normalize_text(&s)),
                        reason: None,
                    };
                }
                Err(_) => {
                    return ExtractResult {
                        value: None,
                        reason: Some("JS 执行结果不可读".to_string()),
                    };
                }
            },
            Err(e) => {
                return ExtractResult {
                    value: None,
                    reason: Some(format!("JS 执行失败：{e}")),
                };
            }
        }
    }

    // 文本模式：innerText，空则 textContent 兜底。
    let text_expr = format!(
        "(()=>{{const el={nth_expr};if(!el)return null;const it=el.innerText||'';const t=it.trim()?it:(el.textContent||'');return t}})()"
    );
    match eval_string(page, &text_expr).await {
        None => ExtractResult {
            value: None,
            reason: Some("元素句柄不可用".to_string()),
        },
        Some(t) if t.is_empty() => ExtractResult {
            value: Some(String::new()),
            reason: Some(
                "选择器匹配到元素，但其文本为空（多半指向了纯装饰节点，试试上一级）".to_string(),
            ),
        },
        Some(t) => ExtractResult {
            value: Some(normalize_text(&t)),
            reason: None,
        },
    }
}

/// 列表页中的单个帖子项：帖子 ID、标题、可点击的绝对 URL。
#[derive(Debug, Clone, PartialEq)]
pub struct ListItem {
    pub post_id: String,
    pub title: String,
    pub url: String,
}

pub(crate) fn extract_id(href: &str, pattern: Option<&regex::Regex>) -> Option<String> {
    if let Some(pattern) = pattern {
        let m = pattern.captures(href)?;
        return m.get(1).map(|g| g.as_str().to_string());
    }
    let path = url::Url::parse(href)
        .map(|u| u.path().trim_end_matches('/').to_string())
        .unwrap_or_else(|_| href.trim_end_matches('/').to_string());
    if path.is_empty() {
        return None;
    }
    path.rsplit('/')
        .next()
        .filter(|s| !s.is_empty())
        .map(|s| s.to_string())
}

/// 提取列表页每个帖子的 (ID, 标题, 绝对 URL)。
///
/// 缺 href 或提取不到 ID 的链接跳过；选择器无匹配时返回空列表，交由上层归入失败路径。
pub async fn extract_list_items(
    page: &Page,
    watch: &WatchTarget,
    timeout: Duration,
) -> Vec<ListItem> {
    let any_expr = any_selector_expr(&watch.link_selector, watch.effective_selector_type());
    if !wait_attached(page, &any_expr, timeout).await {
        return Vec::new();
    }

    let pattern = watch
        .id_pattern
        .as_deref()
        .and_then(|p| regex::Regex::new(p).ok());
    let base = url::Url::parse(&watch.url).ok();

    let items_expr = all_selector_expr(&watch.link_selector, watch.effective_selector_type());
    // 一次性在页面内展开全部 (href, text) 对，避免逐项 round-trip。
    let expr = format!(
        "(()=>{{const els={items_expr};return els.map(a=>({{href:a.getAttribute('href')||'',text:(a.innerText&&a.innerText.trim())?a.innerText:(a.textContent||'')}}))}})()"
    );
    let raw: Vec<serde_json::Value> = match page.evaluate_expression(expr).await {
        Ok(r) => r.into_value().unwrap_or_default(),
        Err(_) => return Vec::new(),
    };

    let mut items = Vec::new();
    for entry in raw {
        let href = entry.get("href").and_then(|v| v.as_str()).unwrap_or("");
        let text = entry.get("text").and_then(|v| v.as_str()).unwrap_or("");
        if href.is_empty() {
            tracing::debug!("列表项缺少 href，跳过：watch={}", watch.name);
            continue;
        }
        let Some(post_id) = extract_id(href, pattern.as_ref()) else {
            tracing::debug!(
                "列表项无法提取帖子 ID，跳过：watch={} href={}",
                watch.name,
                href
            );
            continue;
        };
        let abs_url = match &base {
            Some(b) => match b.join(href) {
                Ok(joined) => joined.to_string(),
                Err(_) => href.to_string(),
            },
            None => href.to_string(),
        };
        items.push(ListItem {
            post_id,
            title: normalize_text(text),
            url: abs_url,
        });
    }
    items
}

/// 把相对 URL 补全为绝对 URL（公开给测试用）。
pub fn urljoin(base: &str, href: &str) -> String {
    match url::Url::parse(base).ok().and_then(|b| b.join(href).ok()) {
        Some(joined) => joined.to_string(),
        None => href.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_normalize_text() {
        assert_eq!(normalize_text("  a \n\t b   c "), "a b c");
        assert_eq!(normalize_text("充分\n  空白"), "充分 空白");
        assert_eq!(normalize_text(""), "");
    }

    #[test]
    fn test_extract_id_default_path_tail() {
        assert_eq!(
            extract_id("/post-911200-1", None),
            Some("post-911200-1".to_string())
        );
        assert_eq!(extract_id("/a/b/123/", None), Some("123".to_string()));
        assert_eq!(extract_id("", None), None);
        assert_eq!(extract_id("/", None), None);
    }

    #[test]
    fn test_extract_id_pattern_captures_group_one() {
        let re = regex::Regex::new(r"post-(\d+)-").unwrap();
        assert_eq!(
            extract_id("/post-911200-1", Some(&re)),
            Some("911200".to_string())
        );
    }

    #[test]
    fn test_extract_id_pattern_without_group_returns_none() {
        let re = regex::Regex::new(r"post-\d+-").unwrap();
        assert_eq!(extract_id("/post-911200-1", Some(&re)), None);
    }

    #[test]
    fn test_urljoin() {
        assert_eq!(urljoin("https://e.com/list", "/a/b"), "https://e.com/a/b");
        assert_eq!(
            urljoin(
                "https://www.nodeseek.com/",
                "https://www.nodeseek.com/post-1-1"
            ),
            "https://www.nodeseek.com/post-1-1"
        );
    }
}
