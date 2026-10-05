//! Visible, opt-in summary-sharing controls for the Windows Tauri package.
//!
//! This window is separate from the ActivityWatch dashboard served over HTTP.
//! It can inspect local state, make a one-off *local-only* preview, or stop
//! sharing. Enabling or uploading is deliberately not exposed to this window.

use log::{info, warn};
use std::io::Write;
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::sync::OnceLock;
use tauri::menu::MenuItem;
use tauri::{AppHandle, Manager, WebviewUrl, WebviewWindow, WebviewWindowBuilder, Wry};

#[cfg(target_os = "windows")]
use std::os::windows::process::CommandExt;
#[cfg(target_os = "windows")]
use winapi::um::winbase::CREATE_NO_WINDOW;

pub const WINDOW_LABEL: &str = "summary-share";
pub const URI_SCHEME: &str = "aw-summary-share";
pub const MENU_ID: &str = "summary_share";
pub const SUMMARY_HTML: &str = include_str!("../assets/summary-share.html");

struct LocalServer {
    port: u16,
    api_key: Option<String>,
}

static LOCAL_SERVER: OnceLock<LocalServer> = OnceLock::new();

/// Bind the controls to the exact server started by this Tauri instance.
/// Other profiles have independent ActivityWatch data but the Python client's
/// current consent file is not profile-aware, so their menu fails closed.
pub fn bind_local_server(profile: &str, port: u16, api_key: Option<&str>) {
    if !crate::profile::is_default(profile) || port == 0 {
        info!("Summary sharing controls require a default-profile local server");
        return;
    }
    if LOCAL_SERVER
        .set(LocalServer {
            port,
            api_key: api_key.map(str::to_owned),
        })
        .is_err()
    {
        warn!("Summary sharing server was already configured");
    }
}

/// Only a bundled, adjacent executable may be launched. No executable path,
/// command line, endpoint, or token is accepted from the WebView.
fn bundled_client() -> Option<PathBuf> {
    LOCAL_SERVER.get()?;
    #[cfg(target_os = "windows")]
    {
        let candidate = std::env::current_exe()
            .ok()?
            .parent()?
            .join("share-client")
            .join("aw-share.exe");
        candidate.is_file().then_some(candidate)
    }
    #[cfg(not(target_os = "windows"))]
    {
        None
    }
}

/// Both the initial and rebuilt tray menus call this helper. The upstream
/// Tauri app has no sharing entry unless the fork package includes aw-share.
pub fn menu_item(app: &AppHandle) -> Option<MenuItem<Wry>> {
    bundled_client()?;
    MenuItem::with_id(app, MENU_ID, "Summary Sharing…", true, None::<&str>).ok()
}

pub fn show_window(app: &AppHandle) {
    if bundled_client().is_none() {
        warn!("Summary sharing client is not bundled next to aw-tauri");
        return;
    }
    if let Some(window) = app.get_webview_window(WINDOW_LABEL) {
        let _ = window.show();
        let _ = window.set_focus();
        return;
    }

    let url = match format!("{URI_SCHEME}://localhost/").parse() {
        Ok(url) => url,
        Err(err) => {
            warn!("Invalid summary-sharing window URL: {err}");
            return;
        }
    };
    match WebviewWindowBuilder::new(app, WINDOW_LABEL, WebviewUrl::CustomProtocol(url))
        .title("ActivityWatch Summary Sharing")
        .inner_size(660.0, 600.0)
        .resizable(true)
        .center()
        .visible(true)
        .build()
    {
        Ok(_) => info!("Opened the local summary-sharing controls"),
        Err(err) => warn!("Could not open the summary-sharing controls: {err}"),
    }
}

/// A small, literal whitelist protects against passing arbitrary subprocess
/// arguments. The matching confirmation is checked again in Rust even though
/// the bundled page asks the person to type it.
fn action_confirmation(action: &str) -> Result<Option<&'static str>, String> {
    match action {
        "status" => Ok(None),
        "local-preview" => Ok(Some("我同意本机预览")),
        "pause" => Ok(Some("暂停分享")),
        "revoke" => Ok(Some("撤回授权")),
        _ => Err("This summary-sharing action is not available".into()),
    }
}

fn run_client(action: &str, confirmation: Option<&str>) -> Result<String, String> {
    let expected = action_confirmation(action)?;
    if expected != confirmation {
        return Err("The requested action was not confirmed locally".into());
    }
    let executable = bundled_client().ok_or("The bundled summary client is missing")?;
    let directory = executable
        .parent()
        .ok_or("Could not locate the bundled summary client directory")?;
    let server = LOCAL_SERVER
        .get()
        .ok_or("The matching ActivityWatch server is not available")?;
    let mut command = Command::new(&executable);
    command
        .arg(action)
        .current_dir(directory)
        .env("PYTHONIOENCODING", "utf-8")
        // Status and consent changes do not need local API credentials.
        .env_remove("AW_SHARE_AW_URL")
        .env_remove("AW_SHARE_AW_API_KEY")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    if action == "local-preview" {
        // Never inherit a stale user override pointing at another local
        // instance. The Python client validates that this URL is loopback.
        command.env(
            "AW_SHARE_AW_URL",
            format!("http://127.0.0.1:{}/api/0", server.port),
        );
        if let Some(api_key) = &server.api_key {
            // Child-process environment only; never an argument, log, or
            // WebView response. It authorizes this one local-only preview.
            command.env("AW_SHARE_AW_API_KEY", api_key);
        }
    }
    #[cfg(target_os = "windows")]
    command.creation_flags(CREATE_NO_WINDOW);

    let output = if action == "local-preview" {
        // The existing Python client continues to require its explicit local
        // preview phrase. Piped UTF-8 input is never a token or URL.
        let mut child = command
            .stdin(Stdio::piped())
            .spawn()
            .map_err(|err| format!("Could not start the summary client: {err}"))?;
        let mut stdin = child.stdin.take().ok_or("Could not confirm the local preview")?;
        let write_result = stdin.write_all("我同意本机预览\n".as_bytes());
        drop(stdin);
        let output = child
            .wait_with_output()
            .map_err(|err| format!("Could not read the summary client result: {err}"))?;
        write_result.map_err(|err| format!("Could not confirm the local preview: {err}"))?;
        output
    } else {
        command
            .stdin(Stdio::null())
            .output()
            .map_err(|err| format!("Could not run the summary client: {err}"))?
    };

    if !output.status.success() {
        let error = String::from_utf8_lossy(&output.stderr);
        let message = error.trim();
        return Err(if message.is_empty() {
            format!("Summary client exited with status {}", output.status)
        } else {
            message.to_string()
        });
    }
    // Keep an unexpectedly large local report from overflowing the WebView.
    if output.stdout.len() > 1_000_000 {
        return Err("The local result is too large to display".into());
    }
    String::from_utf8(output.stdout)
        .map(|output| output.trim().to_string())
        .map_err(|_| "The summary client returned invalid UTF-8".into())
}

#[tauri::command]
pub async fn summary_share_action(
    window: WebviewWindow,
    action: String,
    confirmation: Option<String>,
) -> Result<String, String> {
    // Tauri capability alone is not the only boundary: the HTTP dashboard's
    // main WebView must never invoke a command that can read or change consent.
    if window.label() != WINDOW_LABEL {
        return Err("Summary controls are available only in their local window".into());
    }
    tauri::async_runtime::spawn_blocking(move || run_client(&action, confirmation.as_deref()))
        .await
        .map_err(|err| format!("Summary client task failed: {err}"))?
}

#[cfg(test)]
mod tests {
    use super::action_confirmation;

    #[test]
    fn only_visible_local_actions_are_allowed() {
        assert_eq!(action_confirmation("status"), Ok(None));
        assert_eq!(action_confirmation("local-preview"), Ok(Some("我同意本机预览")));
        assert_eq!(action_confirmation("pause"), Ok(Some("暂停分享")));
        assert_eq!(action_confirmation("revoke"), Ok(Some("撤回授权")));
        assert!(action_confirmation("enable").is_err());
        assert!(action_confirmation("send").is_err());
        assert!(action_confirmation("watch").is_err());
        assert!(action_confirmation("../../other.exe").is_err());
    }
}
