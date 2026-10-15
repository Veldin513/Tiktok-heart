use std::{
    io::{BufRead, BufReader},
    net::TcpListener,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::{mpsc, Arc, Mutex},
    thread,
    time::Duration,
};

use tauri::{Manager, WebviewUrl, WebviewWindowBuilder};

const BACKEND_PORT: &str = "5874";
const INSTANCE_LOCK_ADDR: &str = "127.0.0.1:5873";

struct SingleInstanceGuard {
    _listener: TcpListener,
}

impl SingleInstanceGuard {
    fn acquire() -> Option<Self> {
        TcpListener::bind(INSTANCE_LOCK_ADDR)
            .ok()
            .map(|listener| Self {
                _listener: listener,
            })
    }
}

struct BackendProcess(Arc<Mutex<Option<Child>>>);

impl BackendProcess {
    fn new(child: Option<Child>) -> Self {
        Self(Arc::new(Mutex::new(child)))
    }

    fn stop(&self) {
        let Ok(mut guard) = self.0.lock() else {
            return;
        };
        if let Some(mut child) = guard.take() {
            kill_backend_process_tree(&mut child);
            let _ = child.wait();
        }
    }
}

impl Drop for BackendProcess {
    fn drop(&mut self) {
        self.stop();
    }
}

#[cfg(windows)]
fn hide_console(command: &mut Command) {
    use std::os::windows::process::CommandExt;
    const CREATE_NO_WINDOW: u32 = 0x08000000;
    command.creation_flags(CREATE_NO_WINDOW);
}

#[cfg(not(windows))]
fn hide_console(_command: &mut Command) {}

#[cfg(windows)]
fn kill_backend_process_tree(child: &mut Child) {
    let pid = child.id().to_string();
    let mut command = Command::new("taskkill");
    command
        .args(["/PID", pid.as_str(), "/T", "/F"])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    hide_console(&mut command);
    match command.status() {
        Ok(status) if status.success() => {}
        _ => {
            let _ = child.kill();
        }
    }
}

#[cfg(not(windows))]
fn kill_backend_process_tree(child: &mut Child) {
    let _ = child.kill();
}

fn project_root() -> PathBuf {
    if let Ok(value) = std::env::var("YARA_PROJECT_ROOT") {
        let path = PathBuf::from(value);
        if path.join("app_shell").join("server.py").exists() {
            return path;
        }
    }
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .map(Path::to_path_buf)
        .unwrap_or_else(|| PathBuf::from("."))
}

fn python_executable() -> PathBuf {
    if let Ok(value) = std::env::var("YARA_PYTHON") {
        let path = PathBuf::from(value);
        if path.exists() {
            return path;
        }
    }
    if let Ok(local_app_data) = std::env::var("LOCALAPPDATA") {
        let local = PathBuf::from(local_app_data);
        for candidate in [
            local
                .join("Python")
                .join("pythoncore-3.14-64")
                .join("python.exe"),
            local.join("Python").join("bin").join("python.exe"),
            local
                .join("Programs")
                .join("Python")
                .join("Python314")
                .join("python.exe"),
            local
                .join("Python")
                .join("pythoncore-3.13-64")
                .join("python.exe"),
            local
                .join("Programs")
                .join("Python")
                .join("Python313")
                .join("python.exe"),
            local
                .join("Python")
                .join("pythoncore-3.12-64")
                .join("python.exe"),
            local
                .join("Programs")
                .join("Python")
                .join("Python312")
                .join("python.exe"),
        ] {
            if candidate.exists() {
                return candidate;
            }
        }
    }
    if let Ok(program_files) = std::env::var("ProgramFiles") {
        let root = PathBuf::from(program_files);
        for candidate in [
            root.join("Python314").join("python.exe"),
            root.join("Python313").join("python.exe"),
            root.join("Python312").join("python.exe"),
        ] {
            if candidate.exists() {
                return candidate;
            }
        }
    }
    PathBuf::from("python")
}

fn start_backend() -> Result<(String, BackendProcess), String> {
    let root = project_root();
    let script = root.join("app_shell").join("server.py");
    if !script.exists() {
        return Err(format!("Backend script not found: {}", script.display()));
    }

    let mut command = Command::new(python_executable());
    command
        .arg(&script)
        .arg("--port")
        .arg(BACKEND_PORT)
        .current_dir(&root)
        .env("PYTHONUNBUFFERED", "1")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    hide_console(&mut command);

    let mut child = command
        .spawn()
        .map_err(|error| format!("Failed to start Python backend: {error}"))?;
    let stdout = child.stdout.take();
    let stderr = child.stderr.take();

    if let Some(stderr) = stderr {
        thread::spawn(move || {
            let reader = BufReader::new(stderr);
            for line in reader.lines().map_while(Result::ok) {
                eprintln!("[yara-backend] {line}");
            }
        });
    }

    let (sender, receiver) = mpsc::channel();
    if let Some(stdout) = stdout {
        thread::spawn(move || {
            let reader = BufReader::new(stdout);
            let mut url_sender = Some(sender);
            for line in reader.lines().map_while(Result::ok) {
                if let Some(url) = line.strip_prefix("TikTok Heart Desktop: ") {
                    if let Some(sender) = url_sender.take() {
                        let _ = sender.send(url.trim().to_string());
                    }
                }
            }
        });
    }

    let url = match receiver.recv_timeout(Duration::from_secs(15)) {
        Ok(url) => url,
        Err(error) => {
            let _ = child.kill();
            let _ = child.wait();
            return Err(format!("Backend did not report its URL in time: {error}"));
        }
    };
    Ok((url, BackendProcess::new(Some(child))))
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let Some(instance_guard) = SingleInstanceGuard::acquire() else {
        return;
    };

    tauri::Builder::default()
        .manage(instance_guard)
        .setup(|app| {
            let (backend_url, backend_process) =
                start_backend().map_err(|error| tauri::Error::Anyhow(anyhow::anyhow!(error)))?;
            let parsed_url = url::Url::parse(&backend_url).map_err(|error| {
                tauri::Error::Anyhow(anyhow::anyhow!(format!(
                    "Invalid backend URL {backend_url}: {error}"
                )))
            })?;

            app.manage(backend_process);
            let window = WebviewWindowBuilder::new(app, "main", WebviewUrl::External(parsed_url))
                .title("TikTok Heart")
                .inner_size(1280.0, 820.0)
                .min_inner_size(980.0, 680.0)
                .resizable(true)
                .always_on_top(false)
                .build()?;
            let _ = window.set_always_on_top(false);
            Ok(())
        })
        .on_window_event(|window, event| {
            if window.label() != "main" {
                return;
            }
            if matches!(event, tauri::WindowEvent::CloseRequested { .. }) {
                let state = window.state::<BackendProcess>();
                state.stop();
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running TikTok Heart");
}
