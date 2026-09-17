//! 命令行入口与守护装配。
//!
//! 无子命令时拉起 scheduler + receiver 常驻循环。惰性依赖在 Rust 侧由链接器
//! 天然处理（不运行的代码不产生副作用）。`ConfigError` / `TelegramFatalError`
//! 映射为退出码 2（保留 systemd 的 `RestartPreventExitStatus=2` 契约）。

use std::path::Path;
use std::sync::Arc;

use clap::{Parser, Subcommand};
use hawkeye::config::load_config;
use hawkeye::deploy::{ConnParams, DefaultRemoteOps, DeployArgs};
use hawkeye::notify::TelegramFatalError;
use hawkeye::receive::Receiver;
use hawkeye::scheduler::Scheduler;

/// 网页元素变更监控与 Telegram 通知守护进程
#[derive(Parser, Debug)]
#[command(name = "hawkeye", version)]
struct Args {
    /// 配置文件路径（默认 config.toml）
    #[arg(short, long, default_value = "config.toml")]
    config: String,

    /// 输出调试日志（等价于 --log-level DEBUG）
    #[arg(short, long)]
    verbose: bool,

    /// 日志等级（默认 INFO；与 -v 同时给出时以本项为准）
    /// 设置了 RUST_LOG 时以 RUST_LOG 为准（支持按 target 过滤，如
    /// "info,chromiumoxide::handler=error"）
    #[arg(long)]
    log_level: Option<String>,

    #[command(subcommand)]
    command: Option<Command>,
}

#[derive(Subcommand, Debug)]
enum Command {
    /// 交互式生成本机 config.toml（最小可启动配置）
    Init {
        /// 跳过 Telegram 凭据自检（默认询问）
        #[arg(long)]
        no_verify: bool,
    },
    /// 一键把 Rust 二进制与配置部署到 VPS（打包、上传、远端 install、健康判据）
    Deploy {
        /// VPS 主机地址（默认从 .hawkeye-deploy.toml 读或交互问）
        #[arg(long)]
        host: Option<String>,
        /// SSH 端口（默认 22）
        #[arg(long)]
        port: Option<u16>,
        /// SSH 用户名（默认从 .hawkeye-deploy.toml 读或交互问）
        #[arg(long)]
        user: Option<String>,
        /// 即便服务器上已有真实 config.toml，也用本机的顶掉（旧配置会先备份）
        #[arg(long)]
        overwrite_config: bool,
    },
    /// 把项目打成 dist/hawkeye-<版本>-<时间戳>.zip 部署包
    Package {
        /// 项目根路径（默认当前目录；必须含 rust/Cargo.toml / config.example.toml）
        #[arg(long, default_value = ".")]
        root: String,
    },
}

fn resolve_level(args: &Args) -> tracing::Level {
    if let Some(level) = &args.log_level {
        return match level.to_uppercase().as_str() {
            "DEBUG" => tracing::Level::DEBUG,
            "WARN" | "WARNING" => tracing::Level::WARN,
            "ERROR" => tracing::Level::ERROR,
            _ => tracing::Level::INFO,
        };
    }
    if args.verbose {
        tracing::Level::DEBUG
    } else {
        tracing::Level::INFO
    }
}

/// 解析日志过滤指令：RUST_LOG 优先生效（支持按 target 过滤），否则回退 CLI 级别。
fn resolve_filter_spec(args: &Args, rust_log: Option<&str>) -> String {
    match rust_log.map(str::trim).filter(|s| !s.is_empty()) {
        Some(spec) => spec.to_string(),
        None => resolve_level(args).to_string(),
    }
}

async fn run_daemon(config_path: &str) -> Result<(), DaemonError> {
    let config = load_config(Path::new(config_path)).map_err(DaemonError::Config)?;
    tracing::info!(
        "已加载配置：{} 个商家 / {} 个页面 / {} 个监控元素 / {} 个列表监控",
        config.merchants.len(),
        config.pages().len(),
        config.element_count(),
        config.watches.len()
    );
    if config.pages().is_empty() && config.watches.is_empty() {
        // 零监控不是错误——用户可以先起进程，再在 Telegram 里现场加。
        tracing::warn!("当前未配置任何监控，可在 Telegram 中用 /add 添加");
    }

    let token = config.telegram.bot_token.clone();
    let chat_id = config.telegram.chat_id.clone();
    let notifier = Arc::new(hawkeye::notify::Notifier::new(config.telegram.clone()));

    // 自检放在启动浏览器之前：发不出通知的进程没有必要先拉起 Chromium。
    notifier.verify().await.map_err(DaemonError::Telegram)?;
    // 顺手把快捷菜单建好/对齐；同步失败只告警，不影响监控本体。
    let menu_reply = notifier
        .sync_commands(&hawkeye::control::menu_commands())
        .await;
    tracing::info!("{menu_reply}");

    let browser = Arc::new(hawkeye::fetch::BrowserManager::new(&config));
    browser
        .start()
        .await
        .map_err(|e| DaemonError::Other(anyhow::anyhow!(e)))?;

    // 两条循环共享同一个停止事件：信号处理只需置一次，二者一起收敛。
    // trait 对象的句柄转换：Arc<BrowserManager> → Arc<dyn FetchBackend> 等。
    let (scheduler, stop_rx) = hawkeye::scheduler::Scheduler::new(
        config,
        browser.clone() as Arc<dyn hawkeye::scheduler::FetchBackend>,
        notifier.clone() as Arc<dyn hawkeye::notify::NotifierApi>,
    );
    let controller = Arc::new(hawkeye::control::Controller::new(
        config_path,
        // SchedulerApi 实现在 Arc<Scheduler> 上（reconcile 等方法需要 Arc 语义），
        // 控制面持有 trait 对象时再包一层 Arc。
        Arc::new(Arc::clone(&scheduler)) as Arc<dyn hawkeye::scheduler::SchedulerApi>,
        notifier.clone() as Arc<dyn hawkeye::notify::NotifierApi>,
    ));
    let mut receiver = Receiver::new(&token, &chat_id);

    // 信号处理：SIGTERM / SIGINT → 优雅停止。
    let scheduler_for_signal = Arc::clone(&scheduler);
    tokio::spawn(async move {
        use tokio::signal::unix::{SignalKind, signal};
        let mut sigterm = signal(SignalKind::terminate()).expect("注册 SIGTERM 处理失败");
        let mut sigint = signal(SignalKind::interrupt()).expect("注册 SIGINT 处理失败");
        tokio::select! {
            _ = sigterm.recv() => scheduler_for_signal.request_stop(),
            _ = sigint.recv() => scheduler_for_signal.request_stop(),
        }
    });

    let scheduler_run = Arc::clone(&scheduler);
    let scheduler_task = tokio::spawn(async move { Scheduler::run(scheduler_run).await });
    let controller_clone = Arc::clone(&controller);
    let receiver_task = tokio::spawn(async move {
        let result = receiver
            .run(
                move |command| {
                    let controller = Arc::clone(&controller_clone);
                    async move { controller.handle(command).await }
                },
                stop_rx,
            )
            .await;
        // 接收循环退出（停止或致命错误）时联动停止调度器。
        scheduler.request_stop();
        result
    });

    let (sched_res, recv_res) = tokio::join!(scheduler_task, receiver_task);
    if let Err(e) = sched_res {
        tracing::error!("调度循环异常退出：{e}");
    }
    match recv_res {
        Ok(Ok(())) => {}
        Ok(Err(hawkeye::receive::ReceiveError::Http(e))) => {
            tracing::error!("接收循环异常退出：{e}")
        }
        Ok(Err(hawkeye::receive::ReceiveError::Fatal(e))) => {
            browser.close().await;
            return Err(DaemonError::Telegram(e));
        }
        Err(e) => tracing::error!("接收循环任务异常：{e}"),
    }
    browser.close().await;
    Ok(())
}

enum DaemonError {
    Config(hawkeye::config::ConfigError),
    Telegram(TelegramFatalError),
    Other(anyhow::Error),
}

impl std::fmt::Display for DaemonError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            DaemonError::Config(e) => write!(f, "配置错误：{e}"),
            DaemonError::Telegram(e) => write!(
                f,
                "{e}\n请确认：1) chat_id 正确；2) 该用户已私聊 bot 并发送过 /start（bot 无法主动向陌生用户发起会话）；3) 群聊需先把 bot 拉进群。"
            ),
            DaemonError::Other(e) => write!(f, "{e}"),
        }
    }
}

fn main() {
    let args = Args::parse();
    let level_spec = resolve_filter_spec(&args, std::env::var("RUST_LOG").ok().as_deref());
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::new(level_spec))
        // 显示 target：第三方 crate（如 chromiumoxide）的日志才能溯源归属。
        .with_target(true)
        .init();

    let rt = tokio::runtime::Runtime::new().expect("构建 tokio 运行时失败");
    let code = match args.command {
        None => match rt.block_on(run_daemon(&args.config)) {
            Ok(()) => 0,
            Err(DaemonError::Config(e)) => {
                tracing::error!("{e}");
                2
            }
            Err(DaemonError::Telegram(e)) => {
                tracing::error!("{e}");
                2
            }
            Err(DaemonError::Other(e)) => {
                tracing::error!("{e}");
                1
            }
        },
        Some(Command::Init { no_verify }) => run_init(&args.config, no_verify),
        Some(Command::Deploy {
            host,
            port,
            user,
            overwrite_config,
        }) => rt.block_on(run_deploy_cmd(
            &args.config,
            host,
            port,
            user,
            overwrite_config,
        )),
        Some(Command::Package { root }) => run_package(&root),
    };
    std::process::exit(code);
}

fn run_init(config_path: &str, no_verify: bool) -> i32 {
    let mut ask = hawkeye::wizard::TerminalAsk;
    let mut emit = |msg: &str| println!("{msg}");
    let result = tokio::runtime::Runtime::new()
        .expect("构建 tokio 运行时失败")
        .block_on(hawkeye::wizard::run_wizard(
            Path::new(config_path),
            &mut ask,
            &mut emit,
            !no_verify,
            None,
        ));
    match result {
        Ok(_) => 0,
        Err(e) => {
            eprintln!("init 失败：{e}");
            1
        }
    }
}

/// 生产工厂：russh 连接 + SFTP。独立函数承载返回类型标注，
/// 让 Box::pin 的 async 块能协变到 MakeOpsFuture 的 trait 对象；
/// fn 项对生命周期全称量化，天然满足工厂的 HRTB 签名。
/// known_hosts 路径已由 run_deploy 填进 ConnParams，这里无需再覆盖。
fn make_default_ops(params: ConnParams<'_>) -> hawkeye::deploy::MakeOpsFuture<'_> {
    Box::pin(async move {
        let ops = DefaultRemoteOps::connect(params).await?;
        Ok(Box::new(ops) as Box<dyn hawkeye::deploy::RemoteOps + '_>)
    })
}

async fn run_deploy_cmd(
    config_path: &str,
    host: Option<String>,
    port: Option<u16>,
    user: Option<String>,
    overwrite_config: bool,
) -> i32 {
    let cwd = std::env::current_dir().unwrap_or_else(|_| Path::new(".").to_path_buf());
    let config_path = Path::new(config_path).to_path_buf();
    let connection_file = cwd.join(".hawkeye-deploy.toml");
    let dist_dir = cwd.join("dist");
    let packaging_root = cwd.clone();

    let console = hawkeye::deploy::Console::new(
        |prompt| {
            use std::io::Write;
            print!("{prompt}");
            let _ = std::io::stdout().flush();
            let mut line = String::new();
            let _ = std::io::stdin().read_line(&mut line);
            line.trim_end_matches(['\n', '\r']).to_string()
        },
        |prompt| rpassword::prompt_password(prompt).unwrap_or_default(),
        |msg| println!("{msg}"),
    );

    let deploy_args = DeployArgs {
        host,
        port,
        user,
        overwrite_config,
    };
    let paths = hawkeye::deploy::DeployPaths {
        config_path: &config_path,
        connection_file: &connection_file,
        dist_dir: &dist_dir,
        packaging_root: &packaging_root,
    };
    hawkeye::deploy::run_deploy(
        &deploy_args,
        &paths,
        None,
        &console,
        Box::new(make_default_ops),
    )
    .await
}

fn run_package(root: &str) -> i32 {
    match hawkeye::packaging::build_package(Path::new(root), None, None) {
        Ok(archive) => {
            let pkg_name = archive
                .file_stem()
                .map(|s| s.to_string_lossy().into_owned())
                .unwrap_or_default();
            hawkeye::packaging::print_summary(&archive, &pkg_name);
            0
        }
        Err(e) => {
            eprintln!("[HawkEye] {e}");
            1
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(verbose: bool, log_level: Option<&str>) -> Args {
        Args {
            config: "config.toml".into(),
            verbose,
            log_level: log_level.map(Into::into),
            command: None,
        }
    }

    #[test]
    fn test_rust_log_overrides_cli_level() {
        // RUST_LOG 支持按 target 过滤（如静音 chromiumoxide 的 WS Invalid message）。
        let spec = "info,chromiumoxide::handler=error";
        assert_eq!(resolve_filter_spec(&args(false, None), Some(spec)), spec);
        assert_eq!(
            resolve_filter_spec(&args(true, Some("warn")), Some(spec)),
            spec
        );
    }

    #[test]
    fn test_default_spec_follows_cli_level() {
        assert_eq!(resolve_filter_spec(&args(false, None), None), "INFO");
        assert_eq!(resolve_filter_spec(&args(true, None), None), "DEBUG");
        assert_eq!(
            resolve_filter_spec(&args(false, Some("warn")), None),
            "WARN"
        );
    }

    #[test]
    fn test_blank_rust_log_falls_back_to_cli_level() {
        for blank in ["", "   "] {
            assert_eq!(resolve_filter_spec(&args(false, None), Some(blank)), "INFO");
        }
    }
}
