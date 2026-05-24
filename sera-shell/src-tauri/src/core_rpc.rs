//! Thin HTTP client from the shell to the Python core's P-59 API.
//!
//! Shell → core traffic goes through this one place. No shared DB, no shared
//! state — just signed-bearer HTTP on localhost. Mirrors the OpenAPI surface
//! published at GET /openapi.json.

use serde::{Deserialize, Serialize};

#[derive(Serialize)]
pub struct TurnRequest {
    pub text: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub user_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub channel_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub platform: Option<String>,
}

#[derive(Deserialize, Debug)]
pub struct TurnResponse {
    pub ok: bool,
    pub text: String,
    pub profile_used: Option<String>,
    pub latency_ms: i64,
    pub error: Option<String>,
}

pub struct CoreClient {
    base_url: String,
    bearer: String,
    http: reqwest::Client,
}

impl CoreClient {
    pub fn new(base_url: impl Into<String>, bearer: impl Into<String>) -> Self {
        Self {
            base_url: base_url.into(),
            bearer: bearer.into(),
            http: reqwest::Client::new(),
        }
    }

    /// GET /healthz — used by the shell to wait for the sidecar to come up.
    pub async fn healthz(&self) -> bool {
        let url = format!("{}/healthz", self.base_url);
        match self.http.get(&url).send().await {
            Ok(r) => r.status().is_success(),
            Err(_) => false,
        }
    }

    /// POST /v1/turn — run one agent turn, return the reply.
    pub async fn turn(&self, req: &TurnRequest) -> Result<TurnResponse, String> {
        let url = format!("{}/v1/turn", self.base_url);
        let resp = self
            .http
            .post(&url)
            .bearer_auth(&self.bearer)
            .json(req)
            .send()
            .await
            .map_err(|e| e.to_string())?;
        if !resp.status().is_success() {
            return Err(format!("core returned {}", resp.status()));
        }
        resp.json::<TurnResponse>().await.map_err(|e| e.to_string())
    }
}
