//! Sera desktop shell — Tauri entrypoint.
//!
//! Spawns the self-supervising Python core as a sidecar, waits for /healthz,
//! then exposes one command (`turn`) to the React frontend. All logic lives
//! in the core; the shell is presence + UI only.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod core_process;
mod core_rpc;

use core_process::CoreProcess;
use core_rpc::{CoreClient, TurnRequest};
use std::sync::Arc;
use tauri::State;

const CORE_HOST: &str = "127.0.0.1";
const CORE_PORT: u16 = 11111;

struct AppState {
    core: Arc<CoreProcess>,
    client: CoreClient,
}

#[tauri::command]
async fn turn(text: String, state: State<'_, AppState>) -> Result<core_rpc::TurnResponse, String> {
    let req = TurnRequest {
        text,
        user_id: Some("shell".into()),
        channel_id: Some("shell".into()),
        platform: Some("desktop".into()),
    };
    state.client.turn(&req).await
}

#[tauri::command]
fn core_alive(state: State<'_, AppState>) -> bool {
    state.core.is_alive()
}

fn main() {
    let core = Arc::new(CoreProcess::new(CORE_HOST, CORE_PORT));
    core.spawn().expect("failed to spawn Sera core");

    // The bearer is read from the core's per-install key file at runtime.
    let bearer = std::env::var("SERA_API_KEY").unwrap_or_default();
    let client = CoreClient::new(format!("http://{CORE_HOST}:{CORE_PORT}"), bearer);

    tauri::Builder::default()
        .manage(AppState { core, client })
        .invoke_handler(tauri::generate_handler![turn, core_alive])
        .run(tauri::generate_context!())
        .expect("error while running Sera shell");
}
