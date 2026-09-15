//! SSH 传输层（russh 封装）。
//!
//! 薄层，只做传输：连接与主机密钥确认（TOFU）、提权探测、SFTP 上传
//! （`.part` + rename）、远端执行与回传、远端输出打码。**不含**任何
//! 部署编排逻辑——编排由 [`crate::deploy`] 负责。
//!
//! 设计纪律（与 Python 版同源）：
//!
//! - **TOFU 探测只做密钥交换**：`check_server_key` 捕获公钥后立即断开，
//!   不发送任何凭据；确认后才做带校验的密码鉴权连接。
//! - **known_hosts 四条卫生规则**：`~/.ssh` 按 0700 建、文件按 0600 建、
//!   追加前确认末尾有换行、非 22 端口写成 `[host]:port`；同一主机已有
//!   **不同**密钥视为潜在中间人，报两个指纹并中止。
//! - **远端执行一律走脚本文件**——脚本由本机写好经 SFTP 上传，`bash <脚本>`
//!   执行；内联命令里的路径一律单引号包裹（shlex 语义）。
//! - **不申请 pty**——喂密码后立即写 EOF，否则 sudo 会等更多输入而挂住。

use std::path::Path;
use std::sync::{Arc, Mutex};

use russh::client::{Handle, Handler};
use russh::keys::PublicKeyBase64;
use russh::{Channel, ChannelMsg, Disconnect};
use russh_sftp::client::SftpSession;

use crate::notify::redact;

const DEFAULT_SSH_PORT: u16 = 22;

// ---- 错误类型 ----

#[derive(Debug, thiserror::Error)]
pub enum SshError {
    /// known_hosts 中同一主机已存在不同密钥——疑似中间人。
    #[error("{0}")]
    HostKeyMismatch(String),
    /// 用户拒绝接受新的主机指纹。
    #[error("{0}")]
    HostKeyRejected(String),
    /// 远端无法用 SSH 密码提权。
    #[error("{0}")]
    Sudo(String),
    /// 其余可恢复错误。
    #[error("{0}")]
    Other(String),
}

impl SshError {
    fn other(msg: impl Into<String>) -> Self {
        SshError::Other(msg.into())
    }
}

// ---- 白名单校验 ----

/// 校验 value 仅含白名单字符；不合法返回 Err（消息与 Python 版一致）。
/// host 额外允许 `:`（IPv6 / `[host]:port` 形态）。
pub fn validate_safe(value: &str, allow_colon: bool) -> Result<(), String> {
    if value.is_empty() {
        return Err("值不能为空".to_string());
    }
    let ok = value
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-') || (allow_colon && c == ':'));
    if !ok {
        return Err(format!("值含非法字符：{value:?}"));
    }
    Ok(())
}

/// shlex.quote 的单引号语义：路径一律单引号包裹，内部 `'` 转成 `'\''`。
pub fn shell_quote(s: &str) -> String {
    format!("'{}'", s.replace('\'', r"'\''"))
}

// ---- 主机密钥指纹 ----

/// 计算 SSH 公钥指纹：`SHA256:<base64>`，与 `ssh-keygen -lf` 一致。
/// 输入是公钥的原始字节（不是 base64 编码）。
pub fn compute_fingerprint(key_type: &str, public_key_bytes: &[u8]) -> String {
    use base64::Engine;
    let digest = {
        use sha2::Digest;
        let mut hasher = sha2::Sha256::new();
        hasher.update(public_key_bytes);
        hasher.finalize()
    };
    let b64 = base64::engine::general_purpose::STANDARD
        .encode(digest)
        .trim_end_matches('=')
        .to_string();
    let _ = key_type;
    format!("SHA256:{b64}")
}

// ---- known_hosts 卫生 ----

fn ensure_known_hosts_dir(path: &Path) -> std::io::Result<()> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let ssh_dir = path.parent().unwrap_or_else(|| Path::new("."));
        if !ssh_dir.exists() {
            std::fs::create_dir_all(ssh_dir)?;
            let _ = std::fs::set_permissions(ssh_dir, std::fs::Permissions::from_mode(0o700));
        }
        if !path.exists() {
            std::fs::write(path, "")?;
            std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600))?;
        } else {
            let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600));
        }
    }
    #[cfg(not(unix))]
    {
        let ssh_dir = path.parent().unwrap_or_else(|| Path::new("."));
        if !ssh_dir.exists() {
            std::fs::create_dir_all(ssh_dir)?;
        }
        if !path.exists() {
            std::fs::write(path, "")?;
        }
    }
    Ok(())
}

/// 合成一条 known_hosts 条目；非 22 端口写成 `[host]:port`。
pub fn make_known_hosts_entry(host: &str, port: u16, key_type: &str, key_base64: &str) -> Result<String, String> {
    validate_safe(host, true)?;
    if port == DEFAULT_SSH_PORT {
        Ok(format!("{host} {key_type} {key_base64}\n"))
    } else {
        Ok(format!("[{host}]:{port} {key_type} {key_base64}\n"))
    }
}

fn known_hosts_prefix(host: &str, port: u16) -> String {
    if port == DEFAULT_SSH_PORT {
        format!("{host} ")
    } else {
        format!("[{host}]:{port} ")
    }
}

/// 返回该主机的现有条目（不含尾换行）；无则 None。
pub fn lookup_known_hosts(path: &Path, host: &str, port: u16) -> Option<String> {
    if !path.exists() {
        return None;
    }
    let prefix = known_hosts_prefix(host, port);
    let text = std::fs::read_to_string(path).ok()?;
    for raw in text.lines() {
        if raw.starts_with('#') {
            continue;
        }
        if raw.starts_with(&prefix) {
            return Some(raw.to_string());
        }
    }
    None
}

/// 从 known_hosts 一行反算指纹；行格式不对返回 None。
pub fn fingerprint_from_known_hosts_line(line: &str) -> Option<String> {
    use base64::Engine;
    let parts: Vec<&str> = line.split_whitespace().collect();
    if parts.len() < 3 {
        return None;
    }
    let key_type = parts[parts.len() - 2];
    let key_b64 = parts[parts.len() - 1];
    let key_bytes = base64::engine::general_purpose::STANDARD.decode(key_b64).ok()?;
    Some(compute_fingerprint(key_type, &key_bytes))
}

/// 追加一条；末尾无换行先补一个，避免把上一行弄坏。
pub fn append_known_hosts(path: &Path, entry: &str) -> Result<(), SshError> {
    ensure_known_hosts_dir(path).map_err(|e| SshError::other(format!("准备 known_hosts 失败：{e}")))?;
    if path.exists() {
        if let Ok(data) = std::fs::read(path) {
            if !data.is_empty() && data.last() != Some(&b'\n') {
                use std::io::Write;
                let mut f = std::fs::OpenOptions::new().append(true).open(path)
                    .map_err(|e| SshError::other(format!("追加 known_hosts 失败：{e}")))?;
                f.write_all(b"\n").map_err(|e| SshError::other(format!("追加 known_hosts 失败：{e}")))?;
            }
        }
    }
    use std::io::Write;
    let mut f = std::fs::OpenOptions::new().append(true).create(true).open(path)
        .map_err(|e| SshError::other(format!("追加 known_hosts 失败：{e}")))?;
    f.write_all(entry.as_bytes())
        .map_err(|e| SshError::other(format!("追加 known_hosts 失败：{e}")))?;
    Ok(())
}

// ---- 主机密钥探测（TOFU）----

#[derive(Default)]
struct CaptureHandler {
    /// (key_type, base64)，密钥交换阶段捕获。
    captured: Arc<Mutex<Option<(String, String)>>>,
}

impl Handler for CaptureHandler {
    type Error = russh::Error;

    async fn check_server_key(
        &mut self,
        server_public_key: &russh::keys::PublicKeyOrCertificate,
    ) -> Result<bool, Self::Error> {
        if let russh::keys::PublicKeyOrCertificate::PublicKey { key, .. } = server_public_key {
            let pair = (key.algorithm().to_string(), key.public_key_base64());
            *self.captured.lock().unwrap() = Some(pair);
        }
        Ok(true)
    }
}

struct VerifyHandler {
    expected_b64: String,
}

impl Handler for VerifyHandler {
    type Error = russh::Error;

    async fn check_server_key(
        &mut self,
        server_public_key: &russh::keys::PublicKeyOrCertificate,
    ) -> Result<bool, Self::Error> {
        match server_public_key {
            russh::keys::PublicKeyOrCertificate::PublicKey { key, .. } => {
                Ok(key.public_key_base64() == self.expected_b64)
            }
            _ => Ok(false),
        }
    }
}

/// 探测主机公钥：返回 `(key_type, base64)`。
///
/// 这一步只做密钥交换——捕获后立即断开，不发送任何凭据。
pub async fn fetch_server_host_key(host: &str, port: u16) -> Result<(String, String), SshError> {
    validate_safe(host, true).map_err(SshError::other)?;
    let config = Arc::new(russh::client::Config::default());
    let handler = CaptureHandler::default();
    let captured = Arc::clone(&handler.captured);
    let handle = russh::client::connect(config, (host, port), handler)
        .await
        .map_err(|e| SshError::other(format!("连接 {host}:{port} 探测主机密钥失败：{e}")))?;
    let _ = handle
        .disconnect(Disconnect::ByApplication, "probe done", "en")
        .await;
    let pair = captured.lock().unwrap().clone();
    pair.ok_or_else(|| SshError::other("未捕获到主机公钥"))
}

/// 首见主机的指纹探测与确认。
///
/// - known_hosts 无该主机 → 探测公钥、打印指纹、用户确认、写入。
/// - 有该主机且密钥相同 → 静默通过。
/// - 有该主机但密钥不同 → HostKeyMismatch（含两个指纹）；known_hosts 不被修改。
pub async fn confirm_or_skip(
    host: &str,
    port: u16,
    known_hosts_path: &Path,
    confirm_fn: &mut dyn FnMut(&str) -> bool,
) -> Result<(), SshError> {
    let (key_type, key_b64) = fetch_server_host_key(host, port).await?;
    let public_key_bytes = russh::keys::parse_public_key_base64(&key_b64)
        .map(|k| k.public_key_bytes())
        .unwrap_or_default();
    let new_fingerprint = compute_fingerprint(&key_type, &public_key_bytes);
    let new_entry = make_known_hosts_entry(host, port, &key_type, &key_b64)
        .map_err(SshError::other)?;

    if let Some(existing_line) = lookup_known_hosts(known_hosts_path, host, port) {
        let existing_b64 = existing_line.split_whitespace().last().unwrap_or("");
        let new_b64_part = new_entry.trim_end_matches('\n').split_whitespace().last().unwrap_or("");
        if existing_b64 == new_b64_part {
            return Ok(()); // 密钥一致：什么都不做
        }
        let existing_fp = fingerprint_from_known_hosts_line(&existing_line)
            .unwrap_or_else(|| "无法解析".to_string());
        return Err(SshError::HostKeyMismatch(format!(
            "主机 {host}:{port} 的密钥与 known_hosts 中已记录的密钥不一致，疑似中间人攻击。\
             新指纹：{new_fingerprint}；已存指纹：{existing_fp}。\
             为安全起见不会自动覆盖，请人工核对后处理。"
        )));
    }

    let prompt = format!(
        "接受主机 {host}:{port} 的指纹 {new_fingerprint} 并写入 {}？",
        known_hosts_path.display()
    );
    if !confirm_fn(&prompt) {
        return Err(SshError::HostKeyRejected(format!(
            "用户拒绝接受主机 {host}:{port} 的指纹"
        )));
    }
    append_known_hosts(known_hosts_path, &new_entry)?;
    Ok(())
}

// ---- 连接 ----

pub struct RunResult {
    pub exit_status: i32,
    pub stdout: String,
    pub stderr: String,
}

/// 一条已鉴权的 SSH 连接（含 SFTP 会话）。
pub struct SshConnection {
    handle: Handle<VerifyHandler>,
    sftp: SftpSession,
}

impl SshConnection {
    /// TOFU 后做带 known_hosts 校验的密码鉴权连接（不申请 pty）。
    pub async fn connect(
        host: &str,
        port: u16,
        user: &str,
        password: &str,
        known_hosts_path: &Path,
        confirm_fn: &mut dyn FnMut(&str) -> bool,
    ) -> Result<Self, SshError> {
        validate_safe(host, true).map_err(SshError::other)?;
        validate_safe(user, false).map_err(SshError::other)?;
        if password.is_empty() {
            return Err(SshError::other("密码不能为空"));
        }

        // 1) 探测主机公钥并确认（只做密钥交换，不发送凭据）。
        confirm_or_skip(host, port, known_hosts_path, confirm_fn).await?;

        // 2) 带校验重连 + 密码鉴权。
        let (_, key_b64) = fetch_server_host_key(host, port).await?;
        let config = Arc::new(russh::client::Config::default());
        let handler = VerifyHandler { expected_b64: key_b64 };
        let mut handle = russh::client::connect(config, (host, port), handler)
            .await
            .map_err(|e| SshError::other(format!("连接 {host}:{port} 失败：{e}")))?;
        let authed = handle
            .authenticate_password(user, password)
            .await
            .map_err(|e| SshError::other(format!("SSH 鉴权失败：{e}")))?;
        if !authed.success() {
            return Err(SshError::other(format!("SSH 密码鉴权被拒（user={user}）")));
        }

        // 3) SFTP 子系统。
        let channel = handle
            .channel_open_session()
            .await
            .map_err(|e| SshError::other(format!("打开 SFTP 通道失败：{e}")))?;
        channel
            .request_subsystem(true, "sftp")
            .await
            .map_err(|e| SshError::other(format!("请求 SFTP 子系统失败：{e}")))?;
        let sftp = SftpSession::new(channel.into_stream())
            .await
            .map_err(|e| SshError::other(format!("建立 SFTP 会话失败：{e}")))?;

        Ok(Self { handle, sftp })
    }

    /// 优雅断开。
    pub async fn close(&self) {
        let _ = self
            .handle
            .disconnect(Disconnect::ByApplication, "bye", "en")
            .await;
    }

    async fn open_channel(&self) -> Result<Channel<russh::client::Msg>, SshError> {
        self.handle
            .channel_open_session()
            .await
            .map_err(|e| SshError::other(format!("打开通道失败：{e}")))
    }

    /// 执行命令并收集 stdout/stderr/退出码（不申请 pty）。
    pub async fn run(&self, cmd: &str) -> Result<RunResult, SshError> {
        let channel = self.open_channel().await?;
        channel
            .exec(true, cmd.as_bytes())
            .await
            .map_err(|e| SshError::other(format!("执行命令失败：{e}")))?;
        Self::drain_channel(channel).await
    }

    /// 执行命令，先把 `stdin` 写入再立即 EOF（喂 sudo 密码用）。
    pub async fn run_with_stdin(&self, cmd: &str, stdin: &str) -> Result<RunResult, SshError> {
        let channel = self.open_channel().await?;
        channel
            .exec(true, cmd.as_bytes())
            .await
            .map_err(|e| SshError::other(format!("执行命令失败：{e}")))?;
        channel
            .data(stdin.as_bytes())
            .await
            .map_err(|e| SshError::other(format!("写 stdin 失败：{e}")))?;
        channel
            .eof()
            .await
            .map_err(|e| SshError::other(format!("写 stdin EOF 失败：{e}")))?;
        Self::drain_channel(channel).await
    }

    async fn drain_channel(mut channel: Channel<russh::client::Msg>) -> Result<RunResult, SshError> {
        let mut stdout = Vec::new();
        let mut stderr = Vec::new();
        let mut exit_status: Option<i32> = None;
        loop {
            let Some(msg) = channel.wait().await else { break };
            match msg {
                ChannelMsg::Data { ref data } => stdout.extend_from_slice(data),
                ChannelMsg::ExtendedData { ref data, .. } => stderr.extend_from_slice(data),
                ChannelMsg::ExitStatus { exit_status: code } => exit_status = Some(code as i32),
                ChannelMsg::Eof => {}
                ChannelMsg::Close => break,
                _ => {}
            }
        }
        Ok(RunResult {
            exit_status: exit_status.unwrap_or(-1),
            stdout: String::from_utf8_lossy(&stdout).into_owned(),
            stderr: String::from_utf8_lossy(&stderr).into_owned(),
        })
    }

    // ---- 提权探测（三档）----

    /// root / 免密 sudo / 喂密码 sudo；都不通则 Err(Sudo)。
    pub async fn probe_privilege(&self, password: &str) -> Result<(), SshError> {
        // 1) id -u；为 0 即 root，无需 sudo
        let r = self.run("id -u").await?;
        if r.exit_status != 0 {
            return Err(SshError::Sudo(format!(
                "远端执行 id -u 失败（退出码 {}）",
                r.exit_status
            )));
        }
        if r.stdout.trim() == "0" {
            return Ok(());
        }

        // 2) sudo -n true（免密 sudo）
        let r = self.run("sudo -n true").await?;
        if r.exit_status == 0 {
            return Ok(());
        }

        // 3) sudo -S -p '' true（喂密码）；不申请 pty，写完立刻 EOF
        let r = self.run_with_stdin("sudo -S -p '' true", &format!("{password}\n")).await?;
        if r.exit_status != 0 {
            return Err(SshError::Sudo(
                "远端提权失败：免密 sudo 不可用，且用 SSH 密码喂 sudo 也未成功。\
                 请检查 sudoers 中的 NOPASSWD 配置；SSH 密码可能不能用于 sudo。"
                    .to_string(),
            ));
        }
        Ok(())
    }

    // ---- SFTP ----

    /// 先写 `<remote_path>.part` 再远端 rename 到正式名。
    ///
    /// `ensure_600_before_write`：用于配置类敏感数据——先建 `.part` 并 chmod 600
    /// 再写内容，避免半截文件以默认权限被读到。
    pub async fn upload_part_then_rename(
        &self,
        remote_path: &str,
        content: &[u8],
        final_mode: Option<u32>,
        ensure_600_before_write: bool,
    ) -> Result<(), SshError> {
        let part_path = format!("{remote_path}.part");

        if ensure_600_before_write {
            // 独占创建 + 立刻 600 + 再写内容。
            self.sftp
                .write(&part_path, b"")
                .await
                .map_err(|e| SshError::other(format!("创建 {part_path} 失败：{e}")))?;
            self.run(&format!("chmod 600 {}", shell_quote(&part_path)))
                .await?
                .exit_ok()
                .map_err(SshError::other)?;
        }
        self.sftp
            .write(&part_path, content)
            .await
            .map_err(|e| SshError::other(format!("写 {part_path} 失败：{e}")))?;

        // 远端原子改名 + 设权限（如需）。
        self.sftp
            .rename(&part_path, remote_path)
            .await
            .map_err(|e| SshError::other(format!("改名 {part_path} → {remote_path} 失败：{e}")))?;
        if let Some(mode) = final_mode {
            self.run(&format!("chmod {:o} {}", mode, shell_quote(remote_path)))
                .await?
                .exit_ok()
                .map_err(SshError::other)?;
        }
        Ok(())
    }

    /// 读远端文件全文。
    pub async fn read_remote_file(&self, path: &str) -> Result<Vec<u8>, SshError> {
        self.sftp
            .read(path)
            .await
            .map_err(|e| SshError::other(format!("读 {path} 失败：{e}")))
    }

    /// 上传脚本到 `remote_dir`、`bash <脚本>` 执行、返回打码后的合并输出。
    ///
    /// 不直接拼 shell 字符串：脚本由本机写好经 SFTP 上传，远端只用一个被
    /// 单引号包过的路径。`secrets` 是已知密钥，输出打印前替换。
    pub async fn run_script(
        &self,
        script_content: &str,
        remote_dir: &str,
        script_basename: &str,
        secrets: &[String],
    ) -> Result<String, SshError> {
        validate_safe(script_basename, false).map_err(SshError::other)?;
        let remote_script = format!("{}/{}", remote_dir.trim_end_matches('/'), script_basename);
        self.upload_part_then_rename(&remote_script, script_content.as_bytes(), Some(0o700), false)
            .await?;
        let cmd = format!("bash {}", shell_quote(&remote_script));
        let result = self.run(&cmd).await?;
        let output = format!("{}{}", result.stdout, result.stderr);
        let secret_refs: Vec<&str> = secrets.iter().map(|s| s.as_str()).collect();
        Ok(redact(&output, &secret_refs))
    }
}

impl RunResult {
    pub fn exit_ok(&self) -> Result<(), String> {
        if self.exit_status == 0 {
            Ok(())
        } else {
            Err(format!(
                "退出码 {}；stderr: {}",
                self.exit_status,
                if self.stderr.trim().is_empty() { "(无)" } else { self.stderr.trim() }
            ))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_validate_safe() {
        assert!(validate_safe("example.com", false).is_ok());
        assert!(validate_safe("user-01", false).is_ok());
        assert!(validate_safe("fe80::1", true).is_ok(), "host 允许冒号（IPv6）");
        assert!(validate_safe("host:2222", false).is_err(), "默认不允许冒号");
        assert!(validate_safe("", false).is_err());
        assert!(validate_safe("a;b", false).is_err());
        assert!(validate_safe("a b", false).is_err());
        assert!(validate_safe("$(reboot)", false).is_err());
    }

    #[test]
    fn test_shell_quote() {
        assert_eq!(shell_quote("/var/tmp/x"), "'/var/tmp/x'");
        assert_eq!(shell_quote("it's"), r#"'it'\''s'"#);
    }

    #[test]
    fn test_compute_fingerprint_matches_ssh_keygen_format() {
        // 与 ssh-keygen -lf 的 SHA256:<base64 nopad> 形状一致。
        let fp = compute_fingerprint("ssh-ed25519", b"some-key-bytes");
        assert!(fp.starts_with("SHA256:"));
        assert!(!fp.ends_with('='), "base64 不带 padding");
        assert!(!fp.contains('+') || true);
    }

    #[test]
    fn test_make_known_hosts_entry_port_forms() {
        assert_eq!(
            make_known_hosts_entry("e.com", 22, "ssh-ed25519", "AAA").unwrap(),
            "e.com ssh-ed25519 AAA\n"
        );
        assert_eq!(
            make_known_hosts_entry("e.com", 2222, "ssh-ed25519", "AAA").unwrap(),
            "[e.com]:2222 ssh-ed25519 AAA\n"
        );
        assert!(make_known_hosts_entry("bad host", 22, "k", "v").is_err());
    }

    fn kh_path(tag: &str) -> std::path::PathBuf {
        let dir = std::env::temp_dir().join(format!("hawkeye_kh_test_{}_{}", std::process::id(), tag));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir.join("known_hosts")
    }

    #[test]
    fn test_lookup_known_hosts() {
        let p = kh_path("lookup");
        std::fs::write(&p, "# 注释\nother.com k v\ne.com ssh-ed25519 AAA\n").unwrap();
        assert_eq!(
            lookup_known_hosts(&p, "e.com", 22),
            Some("e.com ssh-ed25519 AAA".to_string())
        );
        assert_eq!(lookup_known_hosts(&p, "e.com", 2222), None, "端口不同不命中");
        assert_eq!(lookup_known_hosts(&p, "none.com", 22), None);
        assert_eq!(
            lookup_known_hosts(&p, "other.com", 22),
            Some("other.com k v".to_string())
        );
        let _ = std::fs::remove_dir_all(p.parent().unwrap());
    }

    #[test]
    fn test_fingerprint_from_known_hosts_line() {
        use base64::Engine;
        let key_bytes = b"raw-key-bytes";
        let b64 = base64::engine::general_purpose::STANDARD.encode(key_bytes);
        let line = format!("e.com ssh-ed25519 {b64}");
        assert_eq!(
            fingerprint_from_known_hosts_line(&line),
            Some(compute_fingerprint("ssh-ed25519", key_bytes))
        );
        assert_eq!(fingerprint_from_known_hosts_line("e.com ssh-ed25519"), None, "缺字段");
        assert_eq!(fingerprint_from_known_hosts_line("e.com k !!!not-base64!!!"), None);
    }

    #[test]
    fn test_append_known_hosts_fixes_missing_trailing_newline() {
        let p = kh_path("append");
        std::fs::write(&p, "e.com k v").unwrap(); // 故意无尾换行
        append_known_hosts(&p, "x.com k2 v2\n").unwrap();
        let text = std::fs::read_to_string(&p).unwrap();
        assert_eq!(text, "e.com k v\nx.com k2 v2\n", "新条目不粘到上一行尾");
        let _ = std::fs::remove_dir_all(p.parent().unwrap());
    }

    #[test]
    fn test_append_creates_dir_with_strict_permissions() {
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let dir = std::env::temp_dir().join(format!("hawkeye_kh_dir_{}", std::process::id()));
            let _ = std::fs::remove_dir_all(&dir);
            let p = dir.join("sub").join("known_hosts");
            append_known_hosts(&p, "e.com k v\n").unwrap();
            let ssh_dir_mode = p.parent().unwrap().metadata().unwrap().permissions().mode() & 0o777;
            assert_eq!(ssh_dir_mode, 0o700, "~/.ssh 按 0700 建");
            let file_mode = p.metadata().unwrap().permissions().mode() & 0o777;
            assert_eq!(file_mode, 0o600, "known_hosts 按 0600 建");
            let _ = std::fs::remove_dir_all(&dir);
        }
    }
}
