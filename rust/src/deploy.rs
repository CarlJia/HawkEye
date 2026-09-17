//! 一键部署编排（`hawkeye deploy`）。
//!
//! 把打包 → 上传 → 远端后台安装 → 跟日志 → 分层健康检查 → 清理串成一条命令，
//! 顺序按「早失败」排——探权限只要几秒，打包上传是分钟级，Chromium 安装是
//! 分钟级。顺序错了用户会在十分钟后才知道自己没有 sudo。
//!
//! 纪律（与 Python 版同源）：
//!
//! - **远端执行一律走脚本文件**——本机生成 launcher 脚本、SFTP 上传后
//!   `bash <脚本>` 执行，不拼 shell 字符串。
//! - **远端暂存目录名来自** `mktemp -d` **的输出**，不是代码里的字面量。
//! - **完成信号三选一**：`.rc` 出现 / `kill -0 <pid>` 失败 / 总时长超上限
//!   （默认 30 分钟）——只等 `.rc` 不够，OOM killer 会让本机永久挂住。
//! - **密码永不落盘**——只走隐藏输入与 launcher 内一次性 `read`。
//! - **本机配置解析失败、占位符 token 漏出都在打包之前退 1**。

use std::cell::RefCell;
use std::future::Future;
use std::path::{Path, PathBuf};
use std::pin::Pin;
use std::time::{Duration, Instant};

use async_trait::async_trait;

use crate::config::{load_raw, parse_config};
use crate::notify::redact;
use crate::packaging::{PLACEHOLDER_TOKEN, PackageError, build_package};
use crate::ssh::{SshConnection, SshError, shell_quote, validate_safe};

// ---- 常量 ----

pub const REMOTE_CONFIG_PATH: &str = "/opt/hawkeye/config.toml";
pub const REMOTE_SERVICE_NAME: &str = "hawkeye.service";
pub const DEFAULT_SSH_PORT: u16 = 22;
pub const DEFAULT_INSTALL_TIMEOUT_SECONDS: f64 = 30.0 * 60.0;
const LOG_POLL_INTERVAL: Duration = Duration::from_secs(2);
const HEALTH_RESTART_OBSERVE_SECONDS: Duration = Duration::from_secs(5);

// 远端三态
pub const CONFIG_STATE_MISSING: &str = "missing";
pub const CONFIG_STATE_PLACEHOLDER: &str = "placeholder";
pub const CONFIG_STATE_REAL: &str = "real";

#[derive(Debug, thiserror::Error)]
#[error("{0}")]
pub struct DeployError(pub String);

/// 后台安装的远端路径与 PID，deploy 拿这些去找尾日志/读 .rc/查存活。
#[derive(Debug, Clone)]
pub struct InstallHandles {
    pub log_path: String,
    pub pid_path: String,
    pub rc_path: String,
    pub pid: u32,
}

/// 连接参数（工厂据此建 RemoteOps）。
pub struct ConnParams<'a> {
    pub host: String,
    pub port: u16,
    pub user: String,
    pub password: String,
    pub known_hosts_path: PathBuf,
    /// TOFU 指纹确认回调（返回 false 拒绝）。可借用 Console（一次部署内活着）。
    pub confirm: Box<dyn FnMut(&str) -> bool + 'a>,
}

pub type BoxedRemoteOps<'a> = Box<dyn RemoteOps + 'a>;
pub type MakeOpsFuture<'a> =
    Pin<Box<dyn Future<Output = Result<BoxedRemoteOps<'a>, SshError>> + 'a>>;
pub type MakeOps<'a> = Box<dyn FnOnce(ConnParams<'a>) -> MakeOpsFuture<'a> + 'a>;

/// 远端操作高层接口。
///
/// [`DefaultRemoteOps`] 用 [`crate::ssh`] + russh 实现真实逻辑；测试注入替身
/// 记录调用并返回罐头数据。编排（本模块）与传输（ssh.rs）解耦，编排可测。
#[async_trait(?Send)]
pub trait RemoteOps {
    async fn probe_privilege(&self) -> Result<(), SshError>;
    async fn probe_remote_config(
        &self,
        config_path: &str,
        placeholder: &str,
    ) -> Result<String, SshError>;
    async fn mktemp_remote_dir(&self) -> Result<String, SshError>;
    async fn upload_zip(&self, local_zip: &Path, remote_path: &str) -> Result<(), SshError>;
    async fn upload_config(&self, local_config: &Path, remote_path: &str) -> Result<(), SshError>;
    async fn run_background_install(
        &self,
        stage_dir: &str,
        pkg_name: &str,
        config_remote_path: Option<&str>,
        overwrite_config: bool,
    ) -> Result<InstallHandles, SshError>;
    async fn tail_log_until_done(
        &self,
        handles: &InstallHandles,
        max_seconds: f64,
        on_line: &mut dyn for<'s> FnMut(&'s str),
    ) -> Result<i32, DeployError>;
    async fn layered_health_check(&self) -> Result<(bool, String), SshError>;
    async fn cleanup_stage(&self, stage_dir: &str, keep_log: bool) -> Result<(), SshError>;
}

/// 面板回调的别名：`Box<dyn FnMut…>` 直接写进结构体字段会触发 type_complexity。
pub type AskFn = Box<dyn FnMut(&str) -> String>;
pub type EmitFn = Box<dyn FnMut(&str)>;

/// 交互面板：ask / ask_secret / emit 三个回调统一承载。
///
/// 用 RefCell 内部可变性：TOFU 确认闭包与编排主体的 emit 同时持有面板，
/// 借用检查器无法表达这种「共享但串行」的用法。
pub struct Console {
    pub ask: RefCell<AskFn>,
    pub ask_secret: RefCell<AskFn>,
    pub emit: RefCell<EmitFn>,
}

impl Console {
    pub fn new(
        ask: impl FnMut(&str) -> String + 'static,
        ask_secret: impl FnMut(&str) -> String + 'static,
        emit: impl FnMut(&str) + 'static,
    ) -> Self {
        Self {
            ask: RefCell::new(Box::new(ask)),
            ask_secret: RefCell::new(Box::new(ask_secret)),
            emit: RefCell::new(Box::new(emit)),
        }
    }

    fn ask(&self, prompt: &str) -> String {
        (self.ask.borrow_mut())(prompt)
    }

    fn ask_secret(&self, prompt: &str) -> String {
        (self.ask_secret.borrow_mut())(prompt)
    }

    fn emit(&self, msg: &str) {
        (self.emit.borrow_mut())(msg);
    }
}

// ---- 连接参数读写 ----

/// 读 `.hawkeye-deploy.toml`；不存在返回空表，解析失败 Err。
fn read_connection_file(path: &Path) -> Result<toml::Table, DeployError> {
    if !path.exists() {
        return Ok(toml::Table::new());
    }
    let text = std::fs::read_to_string(path)
        .map_err(|e| DeployError(format!("读取 {} 失败：{e}", path.display())))?;
    toml::from_str(&text).map_err(|e| DeployError(format!("{} 不是合法 TOML：{e}", path.display())))
}

/// 把 host / port / user 写到 `.hawkeye-deploy.toml`；权限收紧。
/// **不含密码**——密码只经隐藏输入、当次使用，绝不落盘。
fn write_connection_file(path: &Path, host: &str, port: u16, user: &str) -> Result<(), String> {
    let mut payload = toml::Table::new();
    payload.insert("host".into(), toml::Value::String(host.to_string()));
    payload.insert("port".into(), toml::Value::Integer(port as i64));
    payload.insert("user".into(), toml::Value::String(user.to_string()));
    let text = toml::to_string_pretty(&payload).map_err(|e| e.to_string())?;
    std::fs::write(path, text).map_err(|e| e.to_string())?;
    crate::configedit::tighten_permissions(path);
    Ok(())
}

/// 把配置文件里的 port 字段转成 u16；无效返回 None（外层走默认 22）。
fn coerce_port(value: Option<&toml::Value>) -> Option<u16> {
    match value {
        Some(toml::Value::Integer(v)) if *v > 0 && *v <= u16::MAX as i64 => Some(*v as u16),
        Some(toml::Value::String(s)) => s.trim().parse().ok(),
        _ => None,
    }
}

/// 按优先级解析连接参数：命令行 > 配置文件 > 交互。
///
/// `asked_new=true` 表示至少一个值是交互问出来的——外层据此决定是否写回。
fn resolve_connection_params(
    stored: &toml::Table,
    host_arg: Option<&str>,
    user_arg: Option<&str>,
    port_arg: Option<u16>,
    console: &Console,
) -> (String, u16, String, bool) {
    let mut host = host_arg
        .or_else(|| stored.get("host").and_then(|v| v.as_str()))
        .unwrap_or("")
        .trim()
        .to_string();
    let mut user = user_arg
        .or_else(|| stored.get("user").and_then(|v| v.as_str()))
        .unwrap_or("")
        .trim()
        .to_string();
    let mut port = port_arg.or_else(|| coerce_port(stored.get("port")));
    if port.is_none() {
        port = Some(DEFAULT_SSH_PORT);
    }
    let port = port.unwrap();

    let mut asked_new = false;
    if host.is_empty() {
        host = console.ask("请输入 VPS 主机地址：").trim().to_string();
        asked_new = true;
    }
    if user.is_empty() {
        user = console.ask("请输入 SSH 用户名：").trim().to_string();
        asked_new = true;
    }
    (host, port, user, asked_new)
}

// ---- 本机配置占位符硬闸门 ----

/// 校验本机 config.toml 合法且 bot_token 不是占位符。返回 token 供打码。
fn validate_local_config(path: &Path) -> Result<String, String> {
    let raw = load_raw(path).map_err(|e| e.0)?;
    parse_config(&raw).map_err(|e| e.0)?;
    let token = raw
        .get("telegram")
        .and_then(|t| t.get("bot_token"))
        .and_then(|v| v.as_str())
        .unwrap_or("");
    if token.is_empty() || token.contains(PLACEHOLDER_TOKEN) {
        return Err(format!(
            "{} 里的 bot_token 仍是占位符 {PLACEHOLDER_TOKEN:?}。请先运行 `hawkeye init` 填入真实凭据。",
            path.display()
        ));
    }
    Ok(token.to_string())
}

/// 根据远端三态 + --overwrite-config 决定是否采用本机配置。
fn should_adopt_local_config(state: &str, overwrite: bool) -> Result<bool, DeployError> {
    match state {
        CONFIG_STATE_MISSING | CONFIG_STATE_PLACEHOLDER => Ok(true),
        CONFIG_STATE_REAL => Ok(overwrite),
        other => Err(DeployError(format!("未知的远端 config 状态：{other:?}"))),
    }
}

// ---- 摘要 ----

fn print_summary(
    console: &Console,
    success: bool,
    log_path: Option<&str>,
    config_path: Option<&Path>,
) {
    console.emit("");
    if success {
        console.emit("[HawkEye] 部署完成。");
    } else {
        console.emit("[HawkEye] 部署失败；日志路径已打印，可继续排查。");
    }
    if let Some(log) = log_path {
        console.emit(&format!("  安装日志 : {log}"));
        console.emit(&format!("  查看日志 : ssh <user>@<host> 'tail -F {log}'"));
    }
    if let Some(config) = config_path {
        console.emit(&format!("  本机配置 : {}", config.display()));
    }
    console.emit("");
    console.emit("常用运维命令：");
    console.emit("  sudo systemctl status hawkeye.service");
    console.emit("  sudo journalctl -u hawkeye.service -f");
    if !success && log_path.is_some() {
        console.emit("失败回退命令（如自动回滚未生效）：");
        console.emit(
            "  sudo mv /opt/hawkeye/hawkeye.old /opt/hawkeye/hawkeye    # 如果 hawkeye.old 还在",
        );
        console.emit("  sudo systemctl restart hawkeye.service");
    }
}

// ---- launcher 脚本 ----

/// 生成 launcher.sh：setsid nohup 跑 install.sh，.rc/pid/log 三件套信号。
fn build_launcher_script(
    stage_dir: &str,
    pkg_name: &str,
    config_remote_path: Option<&str>,
    overwrite_config: bool,
) -> String {
    let config_part = match config_remote_path {
        Some(p) => format!("--config {} ", shell_quote(p)),
        None => String::new(),
    };
    let overwrite_flag = if overwrite_config {
        "--overwrite-config "
    } else {
        ""
    };
    format!(
        r#"#!/bin/bash
set -e
read -r SUDO_PW
D={stage_dir}
PKG={pkg_name}
export D PKG SUDO_PW
install -m 600 /dev/null "$D/install.log"
setsid nohup bash -c '
echo $$ > "$D/install.pid"
if command -v unzip >/dev/null 2>&1; then
    unzip -q -o "$D/${{PKG}}.zip" -d "$D"
else
    python3 -m zipfile -e "$D/${{PKG}}.zip" "$D"
fi
sudo -S -p "" bash "$D/$PKG/install.sh" install {config_part}{overwrite_flag}\
    >> "$D/install.log" 2>&1 <<< "$SUDO_PW"
rc=$?
echo $rc > "$D/.rc.part"
mv "$D/.rc.part" "$D/.rc"
' </dev/null >/dev/null 2>&1 &
disown
for i in $(seq 1 100); do
    [ -f "$D/install.pid" ] && break
    sleep 0.3
done
cat "$D/install.pid"
"#
    )
}

// ---- 编排主入口 ----

/// 部署选项（CLI 参数的镜像，测试直接构造）。
#[derive(Debug, Clone, Default)]
pub struct DeployArgs {
    pub host: Option<String>,
    pub port: Option<u16>,
    pub user: Option<String>,
    pub overwrite_config: bool,
}

/// 本机侧路径集合。与 [`DeployArgs`] 分开：那几项是命令行选项，这几个由运行环境
/// （配置位置、项目根、dist 目录）决定，成组传递免得签名越过七参。
pub struct DeployPaths<'a> {
    pub config_path: &'a Path,
    pub connection_file: &'a Path,
    pub dist_dir: &'a Path,
    pub packaging_root: &'a Path,
}

/// 编排一次部署；返回退出码（0 成功，1 失败，130 取消）。
pub async fn run_deploy<'a>(
    args: &DeployArgs,
    paths: &DeployPaths<'_>,
    // 预构建二进制；None 时 build_package 自动 cargo build --release（测试传现成文件）。
    binary: Option<&Path>,
    console: &'a Console,
    make_ops: MakeOps<'a>,
) -> i32 {
    console.emit("[HawkEye] 一键部署开始……");

    // 1. 读 .hawkeye-deploy.toml
    let stored = match read_connection_file(paths.connection_file) {
        Ok(s) => s,
        Err(e) => {
            console.emit(&format!(
                "读取 {} 失败：{e}",
                paths.connection_file.display()
            ));
            return 1;
        }
    };

    // 2. 收集 host / port / user
    let (host, port, user, asked_new) = resolve_connection_params(
        &stored,
        args.host.as_deref(),
        args.user.as_deref(),
        args.port,
        console,
    );

    // 3. 白名单校验——畸形 host/user 在连接之前就挡下
    if let Err(e) = validate_safe(&host, true).and_then(|_| validate_safe(&user, false)) {
        console.emit(&format!("连接参数不合法：{e}"));
        return 1;
    }

    // 4. 写回 .hawkeye-deploy.toml（仅当本次新问到值）
    if asked_new && !paths.connection_file.exists() {
        match write_connection_file(paths.connection_file, &host, port, &user) {
            Ok(()) => console.emit(&format!(
                "已写入连接参数：{}",
                paths.connection_file.display()
            )),
            Err(e) => console.emit(&format!(
                "写入 {} 失败（仍继续部署）：{e}",
                paths.connection_file.display()
            )),
        }
    }

    // 5. 隐藏输入密码（仅本轮用，绝不落盘）
    let password = console.ask_secret("请输入 SSH 密码（输入隐藏）：");
    if password.is_empty() {
        console.emit("密码不能为空。");
        return 1;
    }
    let mut secrets = vec![password.clone()];

    // 6. 建立连接（TOFU + 带校验重连 + SFTP）
    let conn_params = ConnParams {
        host: host.clone(),
        port,
        user: user.clone(),
        password: password.clone(),
        known_hosts_path: PathBuf::from(
            std::env::var("HOME")
                .map(|h| format!("{h}/.ssh/known_hosts"))
                .unwrap_or_else(|_| ".ssh/known_hosts".to_string()),
        ),
        confirm: Box::new(default_confirm(console)),
    };
    let ops = match make_ops(conn_params).await {
        Ok(ops) => ops,
        Err(e) => {
            console.emit(&format!("SSH 连接失败：{e}"));
            return 1;
        }
    };

    // 7. 探 id -u / sudo（这一步就能挡住 NOPASSWD 都没配的用户）
    if let Err(e) = ops.probe_privilege().await {
        console.emit(&format!("远端提权探测失败：{e}"));
        return 1;
    }

    // 8. 前置探测远端 config.toml 三态
    let state = match ops
        .probe_remote_config(REMOTE_CONFIG_PATH, PLACEHOLDER_TOKEN)
        .await
    {
        Ok(s) => s,
        Err(e) => {
            console.emit(&format!("远端探测失败：{e}"));
            return 1;
        }
    };
    let adopt_local_config = match should_adopt_local_config(&state, args.overwrite_config) {
        Ok(v) => v,
        Err(e) => {
            console.emit(&e.0);
            return 1;
        }
    };

    // 9. 仅当本机配置本次会被采用：parse_config + 占位符硬闸门
    let mut local_config_for_upload: Option<PathBuf> = None;
    if adopt_local_config {
        if !paths.config_path.exists() {
            console.emit(&format!(
                "本机配置 {} 不存在。请先运行 `hawkeye init`，或确认服务器是否已配好。",
                paths.config_path.display()
            ));
            return 1;
        }
        match validate_local_config(paths.config_path) {
            Ok(bot_token) => secrets.push(bot_token),
            Err(e) => {
                console.emit(&e);
                return 1;
            }
        }
        local_config_for_upload = Some(paths.config_path.to_path_buf());
    } else {
        console.emit(
            "服务器上已有真实 config.toml，本次保留不覆盖。\
             你本机的 config.toml 未生效；如需覆盖请加 --overwrite-config。",
        );
    }

    // 10. 打包（白名单 + 泄漏兜底；中止即退 1）
    console.emit("[HawkEye] 打包中……");
    let archive = match build_package(paths.packaging_root, Some(paths.dist_dir), binary) {
        Ok(a) => a,
        Err(PackageError(e)) => {
            console.emit(&format!("打包失败：{e}"));
            return 1;
        }
    };
    let pkg_name = archive
        .file_stem()
        .map(|s| s.to_string_lossy().into_owned())
        .unwrap_or_default();
    if let Err(e) = validate_safe(&pkg_name, false) {
        // 兜底：版本号即便异常也不能落进远端命令
        console.emit(&format!("包名不合法：{e}"));
        return 1;
    }

    // 11. 远端暂存目录（mktemp -d，路径来自命令输出）
    let stage_dir = match ops.mktemp_remote_dir().await {
        Ok(d) => d,
        Err(e) => {
            console.emit(&format!("创建远端暂存目录失败：{e}"));
            return 1;
        }
    };
    console.emit(&format!("远端暂存目录：{stage_dir}"));

    // 12. 上传 zip（先 .part 再 rename）
    let remote_zip = format!("{stage_dir}/{pkg_name}.zip");
    if let Err(e) = ops.upload_zip(&archive, &remote_zip).await {
        console.emit(&format!("上传部署包失败：{e}"));
        return 1;
    }

    // 13. 仅当本机配置本次会被采用：上传 config.toml
    let mut remote_config: Option<String> = None;
    if let Some(local_config) = &local_config_for_upload {
        let rc = format!("{stage_dir}/config.toml");
        if let Err(e) = ops.upload_config(local_config, &rc).await {
            console.emit(&format!("上传配置失败：{e}"));
            return 1;
        }
        remote_config = Some(rc);
    }

    // 14. 远端后台 install（launcher 上传 + 跑 + 读 pid）
    let handles = match ops
        .run_background_install(
            &stage_dir,
            &pkg_name,
            remote_config.as_deref(),
            args.overwrite_config,
        )
        .await
    {
        Ok(h) => h,
        Err(e) => {
            console.emit(&format!("启动远端安装失败：{e}"));
            return 1;
        }
    };
    console.emit(&format!("远端安装日志：{}", handles.log_path));
    console.emit(&format!("远端安装 PID：{}", handles.pid));

    // 15. 跟随日志 + 等待完成（三条退出边）
    console.emit("[HawkEye] 等待远端安装完成（可能十几分钟）……");
    let secret_refs: Vec<String> = secrets.clone();
    let mut on_line = |line: &str| {
        let refs: Vec<&str> = secret_refs.iter().map(|s| s.as_str()).collect();
        console.emit(&redact(line, &refs));
    };
    let rc = match ops
        .tail_log_until_done(&handles, DEFAULT_INSTALL_TIMEOUT_SECONDS, &mut on_line)
        .await
    {
        Ok(rc) => rc,
        Err(e) => {
            console.emit(&e.0);
            print_summary(
                console,
                false,
                Some(&handles.log_path),
                Some(paths.config_path),
            );
            let _ = ops.cleanup_stage(&stage_dir, true).await;
            return 1;
        }
    };
    if rc != 0 {
        console.emit(&format!(
            "远端 install 退出码 {rc}；请查看日志：{}",
            handles.log_path
        ));
        print_summary(
            console,
            false,
            Some(&handles.log_path),
            Some(paths.config_path),
        );
        let _ = ops.cleanup_stage(&stage_dir, true).await;
        return 1;
    }

    // 16. 分层健康判据——.rc=0 也不等于服务在跑
    console.emit("[HawkEye] 检查服务健康……");
    let (ok, msg) = match ops.layered_health_check().await {
        Ok(r) => r,
        Err(e) => (false, format!("无法执行健康检查：{e}")),
    };
    if !ok {
        console.emit(&format!("分层健康检查失败：{msg}"));
        print_summary(
            console,
            false,
            Some(&handles.log_path),
            Some(paths.config_path),
        );
        let _ = ops.cleanup_stage(&stage_dir, true).await;
        return 1;
    }
    console.emit(&format!("健康检查通过：{msg}"));

    // 17. 清理（保留 install.log）
    let _ = ops.cleanup_stage(&stage_dir, true).await;

    print_summary(
        console,
        true,
        Some(&handles.log_path),
        Some(paths.config_path),
    );
    0
}

/// 默认 TOFU 确认函数：先打印提示再问 y/N。
fn default_confirm(console: &Console) -> impl FnMut(&str) -> bool + '_ {
    move |prompt: &str| {
        console.emit(prompt);
        let answer = console
            .ask("  接受？(y/N，回车拒绝)：")
            .trim()
            .to_lowercase();
        ["y", "yes", "是", "确认", "确定", "保存"].contains(&answer.as_str())
    }
}

// ---- 真实远端操作（用 ssh.rs + russh）----

/// 生产用 RemoteOps：用 [`crate::ssh`] + russh 实现。
pub struct DefaultRemoteOps {
    conn: SshConnection,
    password: String,
}

impl DefaultRemoteOps {
    pub async fn connect<'a>(params: ConnParams<'a>) -> Result<Self, SshError> {
        let mut confirm = params.confirm;
        let conn = SshConnection::connect(
            &params.host,
            params.port,
            &params.user,
            &params.password,
            &params.known_hosts_path,
            confirm.as_mut(),
        )
        .await?;
        Ok(Self {
            conn,
            password: params.password,
        })
    }
}

#[async_trait(?Send)]
impl RemoteOps for DefaultRemoteOps {
    async fn probe_privilege(&self) -> Result<(), SshError> {
        self.conn.probe_privilege(&self.password).await
    }

    async fn probe_remote_config(
        &self,
        config_path: &str,
        placeholder: &str,
    ) -> Result<String, SshError> {
        let r = self
            .conn
            .run(&format!("test -f {}", shell_quote(config_path)))
            .await?;
        if r.exit_status != 0 {
            return Ok(CONFIG_STATE_MISSING.to_string());
        }
        let r = self
            .conn
            .run(&format!(
                "grep -qF {} {}",
                shell_quote(placeholder),
                shell_quote(config_path)
            ))
            .await?;
        if r.exit_status == 0 {
            return Ok(CONFIG_STATE_PLACEHOLDER.to_string());
        }
        Ok(CONFIG_STATE_REAL.to_string())
    }

    async fn mktemp_remote_dir(&self) -> Result<String, SshError> {
        // /var/tmp 不受 tmpfiles 短周期清理影响。
        let r = self
            .conn
            .run("mktemp -d /var/tmp/hawkeye-deploy.XXXXXXXXXX")
            .await?;
        if r.exit_status != 0 {
            return Err(SshError::Other(format!(
                "mktemp -d 失败：{}",
                r.stderr.trim()
            )));
        }
        let path = r.stdout.trim().to_string();
        if !path.starts_with("/var/tmp/hawkeye-deploy.") {
            return Err(SshError::Other(format!(
                "mktemp 返回的路径不符合预期：{path:?}"
            )));
        }
        Ok(path)
    }

    async fn upload_zip(&self, local_zip: &Path, remote_path: &str) -> Result<(), SshError> {
        let content = std::fs::read(local_zip)
            .map_err(|e| SshError::Other(format!("读 {} 失败：{e}", local_zip.display())))?;
        self.conn
            .upload_part_then_rename(remote_path, &content, Some(0o600), false)
            .await
    }

    async fn upload_config(&self, local_config: &Path, remote_path: &str) -> Result<(), SshError> {
        let content = std::fs::read(local_config)
            .map_err(|e| SshError::Other(format!("读 {} 失败：{e}", local_config.display())))?;
        self.conn
            .upload_part_then_rename(remote_path, &content, Some(0o600), true)
            .await
    }

    async fn run_background_install(
        &self,
        stage_dir: &str,
        pkg_name: &str,
        config_remote_path: Option<&str>,
        overwrite_config: bool,
    ) -> Result<InstallHandles, SshError> {
        // 上传 launcher；密码经 stdin 一次性喂入（不分配 pty）。
        let launcher =
            build_launcher_script(stage_dir, pkg_name, config_remote_path, overwrite_config);
        let launcher_remote = format!("{stage_dir}/deploy-launcher.sh");
        self.conn
            .upload_part_then_rename(&launcher_remote, launcher.as_bytes(), Some(0o700), false)
            .await?;

        let r = self
            .conn
            .run_with_stdin(
                &format!("bash {}", shell_quote(&launcher_remote)),
                &format!("{}\n", self.password),
            )
            .await?;
        if r.exit_status != 0 {
            let safe_stderr = redact(r.stderr.trim(), &[&self.password]);
            return Err(SshError::Other(format!(
                "launcher 退出码 {}；stderr: {}",
                r.exit_status,
                if safe_stderr.is_empty() {
                    "(无)"
                } else {
                    &safe_stderr
                }
            )));
        }

        // 读 install.pid。
        let pid_bytes = self
            .conn
            .read_remote_file(&format!("{stage_dir}/install.pid"))
            .await?;
        let pid_text = String::from_utf8_lossy(&pid_bytes).trim().to_string();
        let pid: u32 = pid_text
            .parse()
            .map_err(|_| SshError::Other(format!("install.pid 不是数字：{pid_text:?}")))?;

        Ok(InstallHandles {
            log_path: format!("{stage_dir}/install.log"),
            pid_path: format!("{stage_dir}/install.pid"),
            rc_path: format!("{stage_dir}/.rc"),
            pid,
        })
    }

    async fn tail_log_until_done(
        &self,
        handles: &InstallHandles,
        max_seconds: f64,
        on_line: &mut dyn for<'s> FnMut(&'s str),
    ) -> Result<i32, DeployError> {
        let start = Instant::now();
        let mut last_pos: usize = 0;
        loop {
            // 1) 读日志新增内容
            if let Ok(data) = self.conn.read_remote_file(&handles.log_path).await
                && data.len() > last_pos
            {
                let text = String::from_utf8_lossy(&data[last_pos..]).into_owned();
                last_pos = data.len();
                for line in text.lines() {
                    on_line(line);
                }
            }

            // 2) .rc 出现 → 读退出码
            if let Ok(rc_bytes) = self.conn.read_remote_file(&handles.rc_path).await {
                let rc_text = String::from_utf8_lossy(&rc_bytes).trim().to_string();
                if !rc_text.is_empty() {
                    // 半截写入解释为异常退出
                    return Ok(rc_text.parse::<i32>().unwrap_or(-1));
                }
            }

            // 3) pid 已死 → 立刻非零退出（防止 OOM 时永久挂住）
            let r = self
                .conn
                .run(&format!("kill -0 {}", handles.pid))
                .await
                .map_err(|e| DeployError(e.to_string()))?;
            if r.exit_status != 0 {
                return Ok(-1);
            }

            // 4) 超时
            if start.elapsed().as_secs_f64() >= max_seconds {
                return Err(DeployError(format!(
                    "后台安装超时（{max_seconds:.0} 秒）；日志：{}",
                    handles.log_path
                )));
            }

            tokio::time::sleep(LOG_POLL_INTERVAL).await;
        }
    }

    async fn layered_health_check(&self) -> Result<(bool, String), SshError> {
        // 0) 远端是否有 systemctl；没有（容器/非 systemd）直接放过。
        let r = self.conn.run("command -v systemctl").await?;
        if r.exit_status != 0 {
            return Ok((true, "远端无 systemctl，跳过分层健康检查".to_string()));
        }

        // 1) is-enabled 必须 enabled
        let r = self
            .conn
            .run(&format!("systemctl is-enabled {REMOTE_SERVICE_NAME}"))
            .await?;
        let enabled = r.stdout.trim();
        if enabled != "enabled" {
            return Ok((false, format!("服务未启用（is-enabled={enabled}）")));
        }

        // 2) 远端配置就绪性
        let r = self
            .conn
            .run(&format!(
                "grep -qF {} {}",
                shell_quote(PLACEHOLDER_TOKEN),
                shell_quote(REMOTE_CONFIG_PATH)
            ))
            .await?;
        if r.exit_status == 0 {
            return Ok((
                true,
                "服务已启用，配置文件仍是模板（is-active 不要求）".to_string(),
            ));
        }

        // 3) 已配置：is-active 必须 active
        let r = self
            .conn
            .run(&format!("systemctl is-active {REMOTE_SERVICE_NAME}"))
            .await?;
        let status = r.stdout.trim();
        if status != "active" {
            return Ok((false, format!("服务未运行（is-active={status}）")));
        }

        // 4) NRestarts 观察窗内不增长
        let r1 = self
            .conn
            .run(&format!(
                "systemctl show -p NRestarts --value {REMOTE_SERVICE_NAME}"
            ))
            .await?;
        let n1 = r1.stdout.trim().to_string();
        tokio::time::sleep(HEALTH_RESTART_OBSERVE_SECONDS).await;
        let r2 = self
            .conn
            .run(&format!(
                "systemctl show -p NRestarts --value {REMOTE_SERVICE_NAME}"
            ))
            .await?;
        let n2 = r2.stdout.trim().to_string();
        if n1 != n2 {
            return Ok((false, format!("NRestarts 在观察窗内增长（{n1} → {n2}）")));
        }

        // 5) ExecMainStatus=2 单独给诊断（RestartPreventExitStatus=2 让它停在 failed）
        let r3 = self
            .conn
            .run(&format!(
                "systemctl show -p ExecMainStatus --value {REMOTE_SERVICE_NAME}"
            ))
            .await?;
        if r3.stdout.trim() == "2" {
            return Ok((
                false,
                "服务主进程以 2 退出（配置/Telegram 凭据致命错误）；\
                 systemd 单元里 RestartPreventExitStatus=2 已让它停在 failed。"
                    .to_string(),
            ));
        }

        Ok((true, "分层健康检查通过".to_string()))
    }

    async fn cleanup_stage(&self, stage_dir: &str, keep_log: bool) -> Result<(), SshError> {
        // 删远端暂存目录里的内容；保留 install.log。
        let r = self
            .conn
            .run(&format!("ls -1 {}", shell_quote(stage_dir)))
            .await?;
        if r.exit_status != 0 {
            return Ok(());
        }
        for name in r.stdout.lines() {
            let name = name.trim();
            if name.is_empty() {
                continue;
            }
            if keep_log && name == "install.log" {
                continue;
            }
            let _ = self
                .conn
                .run(&format!(
                    "rm -rf {}",
                    shell_quote(&format!("{stage_dir}/{name}"))
                ))
                .await;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;

    // ---- 纯函数 ----

    #[test]
    fn test_coerce_port_handles_various_inputs() {
        assert_eq!(coerce_port(Some(&toml::Value::Integer(22))), Some(22));
        assert_eq!(
            coerce_port(Some(&toml::Value::String("2222".into()))),
            Some(2222)
        );
        assert_eq!(
            coerce_port(Some(&toml::Value::String("  80  ".into()))),
            Some(80)
        );
        assert_eq!(coerce_port(None), None);
        assert_eq!(coerce_port(Some(&toml::Value::String("abc".into()))), None);
        assert_eq!(coerce_port(Some(&toml::Value::Float(1.5))), None);
        assert_eq!(coerce_port(Some(&toml::Value::Integer(99999))), None);
    }

    fn console_with_ask(ask: impl FnMut(&str) -> String + 'static) -> Console {
        Console::new(ask, |_| String::new(), |_| {})
    }

    #[test]
    fn test_resolve_connection_params_cli_over_file() {
        // 命令行 > 配置文件 > 交互。
        let mut stored = toml::Table::new();
        stored.insert("host".into(), toml::Value::String("old.com".into()));
        stored.insert("user".into(), toml::Value::String("olduser".into()));
        stored.insert("port".into(), toml::Value::Integer(2222));
        let console = console_with_ask(|_| panic!("不应有交互输入"));
        let (host, port, user, asked) =
            resolve_connection_params(&stored, Some("cli.com"), None, None, &console);
        assert_eq!(host, "cli.com");
        assert_eq!(port, 2222, "CLI 未指定时从配置文件来");
        assert_eq!(user, "olduser");
        assert!(!asked);
    }

    #[test]
    fn test_resolve_connection_params_falls_back_to_interactive() {
        let answers = RefCell::new(vec!["vps.example.com".to_string(), "root".to_string()]);
        let console = console_with_ask(move |_| answers.borrow_mut().remove(0));
        let (host, port, user, asked) =
            resolve_connection_params(&toml::Table::new(), None, None, None, &console);
        assert_eq!(host, "vps.example.com");
        assert_eq!(port, 22);
        assert_eq!(user, "root");
        assert!(asked, "交互问出的值要写回连接文件");
    }

    #[test]
    fn test_should_adopt_local_config_three_states() {
        assert!(should_adopt_local_config(CONFIG_STATE_MISSING, false).unwrap());
        assert!(should_adopt_local_config(CONFIG_STATE_PLACEHOLDER, false).unwrap());
        assert!(!should_adopt_local_config(CONFIG_STATE_REAL, false).unwrap());
        assert!(should_adopt_local_config(CONFIG_STATE_REAL, true).unwrap());
        assert!(should_adopt_local_config("weird", false).is_err());
    }

    fn write_config_file(dir: &Path, token: &str) -> PathBuf {
        let p = dir.join("config.toml");
        std::fs::write(
            &p,
            format!("[telegram]\nbot_token = \"{token}\"\nchat_id = \"1\"\n\n[[merchants]]\nname = \"m\"\n[[merchants.pages]]\nurl = \"https://e.com\"\n[[merchants.pages.elements]]\nselector = \"#a\"\n"),
        )
        .unwrap();
        p
    }

    #[test]
    fn test_validate_local_config_states() {
        let dir = std::env::temp_dir().join(format!("hawkeye_dep_test_{}_vp", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        // 真实 token → 通过。
        let p = write_config_file(&dir, "8428922140:AA-real");
        assert_eq!(validate_local_config(&p).unwrap(), "8428922140:AA-real");
        // 占位符 → 拒绝。
        let p = write_config_file(&dir, PLACEHOLDER_TOKEN);
        let err = validate_local_config(&p).unwrap_err();
        assert!(err.contains("占位符"), "{err}");
        // 坏 TOML → 拒绝。
        std::fs::write(dir.join("bad.toml"), "坏 {{{").unwrap();
        assert!(validate_local_config(&dir.join("bad.toml")).is_err());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_build_launcher_script() {
        let s = build_launcher_script(
            "/var/tmp/hawkeye-deploy.ABC",
            "hawkeye-0.1.0-x",
            None,
            false,
        );
        assert!(s.contains("D=/var/tmp/hawkeye-deploy.ABC"));
        assert!(s.contains("PKG=hawkeye-0.1.0-x"));
        assert!(s.contains("install.sh\" install"));
        assert!(!s.contains("--config"));
        assert!(!s.contains("--overwrite-config"));
        assert!(s.contains("echo $rc > \"$D/.rc.part\""), "完成信号走 .rc");

        let s = build_launcher_script("/var/tmp/d", "p", Some("/var/tmp/d/config.toml"), true);
        assert!(
            s.contains("--config '/var/tmp/d/config.toml'"),
            "路径单引号包裹：{s}"
        );
        assert!(s.contains("--overwrite-config"));
    }

    #[test]
    fn test_connection_file_roundtrip_without_password() {
        let dir = std::env::temp_dir().join(format!("hawkeye_dep_test_{}_cf", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.join(".hawkeye-deploy.toml");
        write_connection_file(&p, "vps.example.com", 2222, "root").unwrap();
        let stored = read_connection_file(&p).unwrap();
        assert_eq!(
            stored.get("host").and_then(|v| v.as_str()),
            Some("vps.example.com")
        );
        assert_eq!(stored.get("port").and_then(|v| v.as_integer()), Some(2222));
        assert_eq!(stored.get("user").and_then(|v| v.as_str()), Some("root"));
        // 不含密码。
        assert!(
            std::fs::read_to_string(&p)
                .unwrap()
                .find("password")
                .is_none()
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    // ---- FakeRemoteOps ----

    struct FakeRemoteOps {
        privilege_should_fail: bool,
        config_state: &'static str,
        rc: i32,
        health: (bool, String),
        /// tail 阶段喂给 on_line 的行（可含密钥，验证打码）。
        tail_lines: Vec<String>,
        tail_err: Option<String>,
        calls: RefCell<Vec<String>>,
    }

    impl FakeRemoteOps {
        fn new() -> Self {
            Self {
                privilege_should_fail: false,
                config_state: CONFIG_STATE_MISSING,
                rc: 0,
                health: (true, "分层健康检查通过".into()),
                tail_lines: Vec::new(),
                tail_err: None,
                calls: RefCell::new(Vec::new()),
            }
        }

        fn calls(&self) -> Vec<String> {
            self.calls.borrow().clone()
        }

        fn record(&self, name: &str) {
            self.calls.borrow_mut().push(name.to_string());
        }
    }

    #[async_trait(?Send)]
    impl RemoteOps for FakeRemoteOps {
        async fn probe_privilege(&self) -> Result<(), SshError> {
            self.record("probe_privilege");
            if self.privilege_should_fail {
                return Err(SshError::Sudo("远端提权失败".into()));
            }
            Ok(())
        }

        async fn probe_remote_config(
            &self,
            _config_path: &str,
            _placeholder: &str,
        ) -> Result<String, SshError> {
            self.record("probe_remote_config");
            Ok(self.config_state.to_string())
        }

        async fn mktemp_remote_dir(&self) -> Result<String, SshError> {
            self.record("mktemp_remote_dir");
            Ok("/var/tmp/hawkeye-deploy.ABC123".to_string())
        }

        async fn upload_zip(&self, local_zip: &Path, remote_path: &str) -> Result<(), SshError> {
            self.record(&format!(
                "upload_zip:{}:{}",
                local_zip.display(),
                remote_path
            ));
            Ok(())
        }

        async fn upload_config(
            &self,
            _local_config: &Path,
            remote_path: &str,
        ) -> Result<(), SshError> {
            self.record(&format!("upload_config:{remote_path}"));
            Ok(())
        }

        async fn run_background_install(
            &self,
            stage_dir: &str,
            pkg_name: &str,
            config_remote_path: Option<&str>,
            overwrite_config: bool,
        ) -> Result<InstallHandles, SshError> {
            self.record(&format!(
                "run_background_install:{stage_dir}:{pkg_name}:{:?}:{overwrite_config}",
                config_remote_path
            ));
            Ok(InstallHandles {
                log_path: format!("{stage_dir}/install.log"),
                pid_path: format!("{stage_dir}/install.pid"),
                rc_path: format!("{stage_dir}/.rc"),
                pid: 4242,
            })
        }

        async fn tail_log_until_done(
            &self,
            _handles: &InstallHandles,
            _max_seconds: f64,
            on_line: &mut dyn for<'s> FnMut(&'s str),
        ) -> Result<i32, DeployError> {
            self.record("tail_log_until_done");
            for line in &self.tail_lines {
                on_line(line);
            }
            if let Some(err) = &self.tail_err {
                return Err(DeployError(err.clone()));
            }
            Ok(self.rc)
        }

        async fn layered_health_check(&self) -> Result<(bool, String), SshError> {
            self.record("layered_health_check");
            Ok(self.health.clone())
        }

        async fn cleanup_stage(&self, stage_dir: &str, keep_log: bool) -> Result<(), SshError> {
            self.record(&format!("cleanup_stage:{stage_dir}:{keep_log}"));
            Ok(())
        }
    }

    // Rc 共享替身：工厂交出 Rc 克隆，测试侧保留同一实例做断言。
    #[async_trait(?Send)]
    impl RemoteOps for std::rc::Rc<FakeRemoteOps> {
        async fn probe_privilege(&self) -> Result<(), SshError> {
            (**self).probe_privilege().await
        }

        async fn probe_remote_config(
            &self,
            config_path: &str,
            placeholder: &str,
        ) -> Result<String, SshError> {
            (**self).probe_remote_config(config_path, placeholder).await
        }

        async fn mktemp_remote_dir(&self) -> Result<String, SshError> {
            (**self).mktemp_remote_dir().await
        }

        async fn upload_zip(&self, local_zip: &Path, remote_path: &str) -> Result<(), SshError> {
            (**self).upload_zip(local_zip, remote_path).await
        }

        async fn upload_config(
            &self,
            local_config: &Path,
            remote_path: &str,
        ) -> Result<(), SshError> {
            (**self).upload_config(local_config, remote_path).await
        }

        async fn run_background_install(
            &self,
            stage_dir: &str,
            pkg_name: &str,
            config_remote_path: Option<&str>,
            overwrite_config: bool,
        ) -> Result<InstallHandles, SshError> {
            (**self)
                .run_background_install(stage_dir, pkg_name, config_remote_path, overwrite_config)
                .await
        }

        async fn tail_log_until_done(
            &self,
            handles: &InstallHandles,
            max_seconds: f64,
            on_line: &mut dyn for<'s> FnMut(&'s str),
        ) -> Result<i32, DeployError> {
            (**self)
                .tail_log_until_done(handles, max_seconds, on_line)
                .await
        }

        async fn layered_health_check(&self) -> Result<(bool, String), SshError> {
            (**self).layered_health_check().await
        }

        async fn cleanup_stage(&self, stage_dir: &str, keep_log: bool) -> Result<(), SshError> {
            (**self).cleanup_stage(stage_dir, keep_log).await
        }
    }

    // ---- run_deploy 装配 ----

    struct Fixture {
        dir: PathBuf,
        binary: PathBuf,
        remote: std::rc::Rc<FakeRemoteOps>,
        console: Console,
        emitted: std::rc::Rc<RefCell<Vec<String>>>,
    }

    fn fixture(tag: &str) -> Fixture {
        let dir =
            std::env::temp_dir().join(format!("hawkeye_dep_test_{}_{}", std::process::id(), tag));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("rust")).unwrap();
        // 项目根：rust/Cargo.toml + config.example.toml + install.sh + 假二进制。
        std::fs::write(
            dir.join("rust").join("Cargo.toml"),
            "[package]\nname = \"hawkeye\"\nversion = \"0.1.0\"\n",
        )
        .unwrap();
        std::fs::write(
            dir.join("config.example.toml"),
            "[telegram]\nbot_token = \"123456:ABC-your-bot-token\"\nchat_id = \"1\"\n",
        )
        .unwrap();
        // install.sh 是打包根的必备文件（唯一一份安装入口）。
        std::fs::write(dir.join("install.sh"), "#!/bin/sh\n").unwrap();
        let binary = dir.join("fake-bin");
        std::fs::write(&binary, b"fake").unwrap();
        // 本机真实配置。
        write_config_file(&dir, "8428922140:AA-real-token");

        let remote = std::rc::Rc::new(FakeRemoteOps::new());
        let emitted = std::rc::Rc::new(RefCell::new(Vec::new()));
        let emitted_clone = std::rc::Rc::clone(&emitted);
        let console = Console::new(
            |_| "unused".to_string(),
            |_| "ssh-password".to_string(),
            move |m| emitted_clone.borrow_mut().push(m.to_string()),
        );
        Fixture {
            dir,
            binary,
            remote,
            console,
            emitted,
        }
    }

    async fn run(
        f: &Fixture,
        args: &DeployArgs,
        remote: std::rc::Rc<FakeRemoteOps>,
    ) -> (i32, Vec<String>) {
        let config_path = f.dir.join("config.toml");
        let connection_file = f.dir.join(".hawkeye-deploy.toml");
        let dist_dir = f.dir.join("dist");
        let paths = DeployPaths {
            config_path: &config_path,
            connection_file: &connection_file,
            dist_dir: &dist_dir,
            packaging_root: &f.dir,
        };
        let remote_for_factory = std::rc::Rc::clone(&remote);
        let make_ops: MakeOps<'_> = Box::new(move |_params: ConnParams<'_>| {
            let ops = remote_for_factory;
            Box::pin(async move { Ok(Box::new(ops) as BoxedRemoteOps<'_>) })
        });
        let code = run_deploy(args, &paths, Some(&f.binary), &f.console, make_ops).await;
        (code, f.emitted.borrow().clone())
    }

    fn default_args() -> DeployArgs {
        DeployArgs {
            host: Some("vps.example.com".into()),
            port: None,
            user: Some("root".into()),
            overwrite_config: false,
        }
    }

    #[tokio::test]
    async fn test_happy_path_full_flow() {
        let f = fixture("happy");
        let (code, emitted) = run(&f, &default_args(), f.remote.clone()).await;
        assert_eq!(code, 0, "{emitted:?}");
        let calls = f.remote.calls();
        // 顺序：探权限 → 探三态 → mktemp → 上传 zip → 上传配置 → 后台装 → 跟日志 → 健康 → 清理。
        // （upload_zip 的本机路径含时间戳，用通配比较。）
        assert_eq!(calls.len(), 9);
        assert_eq!(calls[0], "probe_privilege");
        assert_eq!(calls[1], "probe_remote_config");
        assert_eq!(calls[2], "mktemp_remote_dir");
        assert!(
            calls[3].starts_with("upload_zip:")
                && calls[3].contains("/var/tmp/hawkeye-deploy.ABC123/hawkeye-0.1.0-")
                && calls[3].ends_with(".zip"),
            "{}",
            calls[3]
        );
        let _ = calls;
        assert_eq!(
            calls[4],
            "upload_config:/var/tmp/hawkeye-deploy.ABC123/config.toml"
        );
        assert!(
            calls[5]
                .starts_with("run_background_install:/var/tmp/hawkeye-deploy.ABC123:hawkeye-0.1.0"),
            "{}",
            calls[5]
        );
        assert_eq!(calls[6], "tail_log_until_done");
        assert_eq!(calls[7], "layered_health_check");
        assert_eq!(
            calls[8],
            "cleanup_stage:/var/tmp/hawkeye-deploy.ABC123:true"
        );
        assert!(
            emitted.iter().any(|l| l.contains("部署完成")),
            "{emitted:?}"
        );
    }

    #[tokio::test]
    async fn test_privilege_failure_exits_early() {
        let f = fixture("priv");
        let mut remote = FakeRemoteOps::new();
        remote.privilege_should_fail = true;
        let remote = std::rc::Rc::new(remote);
        let (code, emitted) = run(&f, &default_args(), remote.clone()).await;
        assert_eq!(code, 1);
        assert!(
            emitted.iter().any(|l| l.contains("远端提权探测失败")),
            "{emitted:?}"
        );
        // 早失败：探权限之后什么都不做。
        assert_eq!(remote.calls(), vec!["probe_privilege"]);
    }

    #[tokio::test]
    async fn test_placeholder_token_blocked_before_packaging() {
        let f = fixture("ph");
        std::fs::write(
            f.dir.join("config.toml"),
            format!("[telegram]\nbot_token = \"{PLACEHOLDER_TOKEN}\"\nchat_id = \"1\"\n"),
        )
        .unwrap();
        let (code, emitted) = run(&f, &default_args(), f.remote.clone()).await;
        assert_eq!(code, 1);
        assert!(emitted.iter().any(|l| l.contains("占位符")), "{emitted:?}");
        // 占位符闸门在打包之前——mktemp 之后的步骤都不该发生。
        assert!(
            !f.remote.calls().iter().any(|c| c.starts_with("upload_zip")),
            "{:?}",
            f.remote.calls()
        );
    }

    #[tokio::test]
    async fn test_real_remote_config_kept_without_overwrite() {
        let f = fixture("real");
        let mut remote = FakeRemoteOps::new();
        remote.config_state = CONFIG_STATE_REAL;
        let remote = std::rc::Rc::new(remote);
        let (code, emitted) = run(&f, &default_args(), remote.clone()).await;
        assert_eq!(code, 0);
        assert!(
            emitted.iter().any(|l| l.contains("保留不覆盖")),
            "{emitted:?}"
        );
        assert!(
            !remote
                .calls()
                .iter()
                .any(|c| c.starts_with("upload_config")),
            "真实配置未要求覆盖时不传"
        );
        // 后台安装不带 config 参数。
        assert!(
            remote
                .calls()
                .iter()
                .any(|c| c.contains("run_background_install") && c.contains(":None:"))
        );
    }

    #[tokio::test]
    async fn test_real_remote_config_overwritten_when_requested() {
        let f = fixture("ow");
        let mut remote = FakeRemoteOps::new();
        remote.config_state = CONFIG_STATE_REAL;
        let remote = std::rc::Rc::new(remote);
        let mut args = default_args();
        args.overwrite_config = true;
        let (code, _) = run(&f, &args, remote.clone()).await;
        assert_eq!(code, 0);
        assert!(
            remote
                .calls()
                .iter()
                .any(|c| c.starts_with("upload_config"))
        );
        assert!(
            remote
                .calls()
                .iter()
                .any(|c| c.contains("run_background_install") && c.contains("true"))
        );
    }

    #[tokio::test]
    async fn test_nonzero_rc_reports_failure() {
        let f = fixture("rc");
        let mut remote = FakeRemoteOps::new();
        remote.rc = 3;
        let remote = std::rc::Rc::new(remote);
        let (code, emitted) = run(&f, &default_args(), remote.clone()).await;
        assert_eq!(code, 1);
        assert!(
            emitted.iter().any(|l| l.contains("退出码 3")),
            "{emitted:?}"
        );
        assert!(
            emitted.iter().any(|l| l.contains("部署失败")),
            "{emitted:?}"
        );
        // 失败路径也要清理（保留日志）。
        assert!(
            remote
                .calls()
                .iter()
                .any(|c| c.contains("cleanup_stage") && c.ends_with(":true"))
        );
    }

    #[tokio::test]
    async fn test_health_check_failure_exits_one() {
        let f = fixture("health");
        let mut remote = FakeRemoteOps::new();
        remote.health = (false, "服务未运行（is-active=failed）".into());
        let remote = std::rc::Rc::new(remote);
        let (code, emitted) = run(&f, &default_args(), remote.clone()).await;
        assert_eq!(code, 1);
        assert!(
            emitted.iter().any(|l| l.contains("服务未运行")),
            "{emitted:?}"
        );
    }

    #[tokio::test]
    async fn test_tail_timeout_exits_one() {
        let f = fixture("timeout");
        let mut remote = FakeRemoteOps::new();
        remote.tail_err = Some("后台安装超时（1800 秒）".into());
        let remote = std::rc::Rc::new(remote);
        let (code, emitted) = run(&f, &default_args(), remote.clone()).await;
        assert_eq!(code, 1);
        assert!(emitted.iter().any(|l| l.contains("超时")), "{emitted:?}");
    }

    #[tokio::test]
    async fn test_log_lines_are_redacted() {
        // 日志行里的 SSH 密码与 bot token 必须打码后才回显。
        let f = fixture("redact");
        let mut remote = FakeRemoteOps::new();
        remote.tail_lines = vec![
            "echo ssh-password".to_string(),
            "token=8428922140:AA-real-token".to_string(),
            "正常行".to_string(),
        ];
        let remote = std::rc::Rc::new(remote);
        let (code, emitted) = run(&f, &default_args(), remote).await;
        assert_eq!(code, 0);
        let joined = emitted.join("\n");
        assert!(
            !joined.contains("ssh-password"),
            "密码不得明文出现：{joined}"
        );
        assert!(
            !joined.contains("8428922140:AA-real-token"),
            "token 不得明文出现"
        );
        assert!(joined.contains("正常行"));
        assert!(joined.contains("<REDACTED>"));
    }

    #[tokio::test]
    async fn test_invalid_host_rejected_before_connect() {
        let f = fixture("badhost");
        let mut args = default_args();
        args.host = Some("bad host; rm -rf".into());
        let (code, emitted) = run(&f, &args, f.remote.clone()).await;
        assert_eq!(code, 1);
        assert!(emitted.iter().any(|l| l.contains("不合法")), "{emitted:?}");
        assert!(f.remote.calls().is_empty(), "畸形 host 在连接之前就挡下");
    }

    #[tokio::test]
    async fn test_empty_password_rejected() {
        let f = fixture("nopw");
        let console = Console::new(|_| String::new(), |_| String::new(), |_| {});
        let make_ops: MakeOps<'_> =
            Box::new(|_p: ConnParams<'_>| Box::pin(async { unreachable!() }));
        let config_path = f.dir.join("config.toml");
        let connection_file = f.dir.join(".hawkeye-deploy.toml");
        let dist_dir = f.dir.join("dist");
        let paths = DeployPaths {
            config_path: &config_path,
            connection_file: &connection_file,
            dist_dir: &dist_dir,
            packaging_root: &f.dir,
        };
        let code = run_deploy(&default_args(), &paths, Some(&f.binary), &console, make_ops).await;
        assert_eq!(code, 1);
    }

    #[tokio::test]
    async fn test_packaging_failure_exits_one() {
        // 项目根缺 config.example.toml → 打包失败 → 1，install 不被触发。
        let f = fixture("pkgfail");
        std::fs::remove_file(f.dir.join("config.example.toml")).unwrap();
        let (code, emitted) = run(&f, &default_args(), f.remote.clone()).await;
        assert_eq!(code, 1);
        assert!(
            emitted.iter().any(|l| l.contains("打包失败")),
            "{emitted:?}"
        );
        assert!(
            !f.remote
                .calls()
                .iter()
                .any(|c| c.starts_with("run_background_install"))
        );
    }

    #[tokio::test]
    async fn test_connection_params_written_back_when_asked() {
        // CLI 未给 host/user 且连接文件不存在 → 交互问出后写回（不含密码）。
        let f = fixture("writeback");
        let answers = std::rc::Rc::new(RefCell::new(vec![
            "vps9.example.com".to_string(),
            "root9".to_string(),
        ]));
        let answers_for_ask = std::rc::Rc::clone(&answers);
        let console = Console::new(
            move |p: &str| {
                if p.contains("主机地址") || p.contains("用户名") {
                    answers_for_ask.borrow_mut().remove(0)
                } else {
                    String::new()
                }
            },
            |_| "ssh-password".to_string(),
            |_| {},
        );
        let remote = std::rc::Rc::new(FakeRemoteOps::new());
        let remote_for_factory = std::rc::Rc::clone(&remote);
        let make_ops: MakeOps<'_> = Box::new(move |_p: ConnParams<'_>| {
            let ops = remote_for_factory;
            Box::pin(async move { Ok(Box::new(ops) as BoxedRemoteOps<'_>) })
        });
        let args = DeployArgs::default();
        let config_path = f.dir.join("config.toml");
        let connection_file = f.dir.join(".hawkeye-deploy.toml");
        let dist_dir = f.dir.join("dist");
        let paths = DeployPaths {
            config_path: &config_path,
            connection_file: &connection_file,
            dist_dir: &dist_dir,
            packaging_root: &f.dir,
        };
        let code = run_deploy(&args, &paths, Some(&f.binary), &console, make_ops).await;
        assert_eq!(code, 0);
        let stored = read_connection_file(&f.dir.join(".hawkeye-deploy.toml")).unwrap();
        assert_eq!(
            stored.get("host").and_then(|v| v.as_str()),
            Some("vps9.example.com")
        );
        assert_eq!(stored.get("user").and_then(|v| v.as_str()), Some("root9"));
        assert!(
            !std::fs::read_to_string(f.dir.join(".hawkeye-deploy.toml"))
                .unwrap()
                .contains("ssh-password")
        );
    }
}
