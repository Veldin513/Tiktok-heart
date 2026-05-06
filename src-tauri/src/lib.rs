use std::{
    io::{BufRead, BufReader, Read, Write},
    net::TcpStream,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::{mpsc, Arc, Mutex},
    thread,
    time::Duration,
};

use tauri::{Manager, WebviewUrl, WebviewWindowBuilder};

const DEFAULT_BACKEND_URL: &str = "http://127.0.0.1:5874/";

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
            let _ = child.kill();
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

fn backend_is_ready(url: &str) -> bool {
    let Some(host_port) = url
        .strip_prefix("http://")
        .and_then(|value| value.split('/').next())
    else {
        return false;
    };
    let Ok(mut stream) = TcpStream::connect_timeout(
        &host_port
            .parse()
            .unwrap_or_else(|_| "127.0.0.1:5874".parse().unwrap()),
        Duration::from_millis(700),
    ) else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(Duration::from_millis(900)));
    let request =
        format!("GET /api/diagnostics HTTP/1.1\r\nHost: {host_port}\r\nConnection: close\r\n\r\n");
    if stream.write_all(request.as_bytes()).is_err() {
        return false;
    }
    let mut response = String::new();
    stream.read_to_string(&mut response).is_ok() && response.contains("200 OK")
}

fn start_backend() -> Result<(String, BackendProcess), String> {
    if backend_is_ready(DEFAULT_BACKEND_URL) {
        return Ok((DEFAULT_BACKEND_URL.to_string(), BackendProcess::new(None)));
    }

    let root = project_root();
    let script = root.join("app_shell").join("server.py");
    if !script.exists() {
        return Err(format!("Backend script not found: {}", script.display()));
    }

    let mut command = Command::new("python");
    command
        .arg(&script)
        .arg("--port")
        .arg("5874")
        .current_dir(&root)
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
            for line in reader.lines().map_while(Result::ok) {
                if let Some(url) = line.strip_prefix("TikTok Heart Desktop: ") {
                    let _ = sender.send(url.trim().to_string());
                    break;
                }
            }
        });
    }

    let url = receiver
        .recv_timeout(Duration::from_secs(8))
        .unwrap_or_else(|_| DEFAULT_BACKEND_URL.to_string());
    Ok((url, BackendProcess::new(Some(child))))
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .setup(|app| {
            let (backend_url, backend_process) =
                start_backend().map_err(|error| tauri::Error::Anyhow(anyhow::anyhow!(error)))?;
            let parsed_url = url::Url::parse(&backend_url).map_err(|error| {
                tauri::Error::Anyhow(anyhow::anyhow!(format!(
                    "Invalid backend URL {backend_url}: {error}"
                )))
            })?;

            app.manage(backend_process);
            WebviewWindowBuilder::new(app, "main", WebviewUrl::External(parsed_url))
                .title("TikTok Heart")
                .inner_size(1280.0, 820.0)
                .min_inner_size(980.0, 680.0)
                .resizable(true)
                .build()?;
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
