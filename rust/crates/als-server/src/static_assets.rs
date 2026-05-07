use axum::{
    extract::{Path as AxumPath, State},
    Json, Router,
    http::header::CONTENT_TYPE,
    http::StatusCode,
    response::{Html, IntoResponse, Response},
    routing::get,
};
use serde_json::json;
use std::path::{Component, Path, PathBuf};
use tower_http::services::ServeDir;

use crate::state::AppState;

pub fn routes(static_dir: &Path) -> Router<AppState> {
    Router::new()
        .route("/", get(index))
        .route("/agent-log", get(agent_log))
        .route("/agent-log/", get(agent_log))
        .route("/manifest.json", get(manifest))
        .route("/sw.js", get(service_worker))
        .route(
            "/api/extensions/{extension_id}/assets/{*asset_path}",
            get(extension_asset),
        )
        .nest_service("/static", ServeDir::new(static_dir))
}

async fn index() -> Html<&'static str> {
    Html(include_str!("index.html"))
}

async fn agent_log() -> Html<&'static str> {
    Html(include_str!("agent_log.html"))
}

async fn manifest() -> Json<serde_json::Value> {
    Json(json!({
        "id": "/",
        "name": "ALS-RS",
        "short_name": "ALS-RS",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#0d0f13",
        "theme_color": "#0d0f13",
        "icons": [{
            "src": "/static/codexas-icon.svg",
            "sizes": "any",
            "type": "image/svg+xml",
            "purpose": "any"
        }]
    }))
}

async fn service_worker() -> impl IntoResponse {
    ([(CONTENT_TYPE, "text/javascript")], include_str!("sw.js"))
}

async fn extension_asset(
    State(state): State<AppState>,
    AxumPath((extension_id, asset_path)): AxumPath<(String, String)>,
) -> Response {
    let Some(path) = extension_asset_path(&state, &extension_id, &asset_path) else {
        return StatusCode::NOT_FOUND.into_response();
    };
    match std::fs::read(&path) {
        Ok(bytes) => ([(CONTENT_TYPE, content_type_for_path(&path))], bytes).into_response(),
        Err(_) => StatusCode::NOT_FOUND.into_response(),
    }
}

fn extension_asset_path(state: &AppState, extension_id: &str, asset_path: &str) -> Option<PathBuf> {
    let entry = state.extensions.get(extension_id)?;
    let parts = safe_asset_components(asset_path)?;
    let first = parts.first()?.as_str();
    if first != "ui" && first != "static" {
        return None;
    }
    let mut path = state.config.extensions_dir.join(entry.path);
    for part in parts {
        path.push(part);
        if std::fs::symlink_metadata(&path)
            .ok()
            .is_some_and(|metadata| metadata.file_type().is_symlink())
        {
            return None;
        }
    }
    let metadata = std::fs::symlink_metadata(&path).ok()?;
    metadata.file_type().is_file().then_some(path)
}

fn safe_asset_components(asset_path: &str) -> Option<Vec<String>> {
    let trimmed = asset_path.trim().trim_start_matches('/');
    if trimmed.is_empty() {
        return None;
    }
    let mut parts = Vec::new();
    for component in Path::new(trimmed).components() {
        match component {
            Component::Normal(part) => {
                let text = part.to_str()?.trim();
                if text.is_empty() {
                    return None;
                }
                parts.push(text.to_owned());
            }
            Component::CurDir => {}
            Component::ParentDir | Component::RootDir | Component::Prefix(_) => return None,
        }
    }
    (!parts.is_empty()).then_some(parts)
}

fn content_type_for_path(path: &Path) -> &'static str {
    match path.extension().and_then(|value| value.to_str()).unwrap_or("") {
        "css" => "text/css; charset=utf-8",
        "html" => "text/html; charset=utf-8",
        "js" | "mjs" => "text/javascript; charset=utf-8",
        "json" => "application/json; charset=utf-8",
        "svg" => "image/svg+xml",
        "txt" => "text/plain; charset=utf-8",
        _ => "application/octet-stream",
    }
}

#[cfg(test)]
mod tests {
    use super::safe_asset_components;

    #[test]
    fn accepts_extension_ui_asset_paths() {
        assert_eq!(
            safe_asset_components("ui/request_cards/card.js"),
            Some(vec![
                "ui".to_owned(),
                "request_cards".to_owned(),
                "card.js".to_owned()
            ])
        );
        assert_eq!(
            safe_asset_components("/static/icon.svg"),
            Some(vec!["static".to_owned(), "icon.svg".to_owned()])
        );
    }

    #[test]
    fn rejects_extension_asset_traversal() {
        assert!(safe_asset_components("../manifest.json").is_none());
        assert!(safe_asset_components("ui/../../manifest.json").is_none());
        assert!(safe_asset_components("").is_none());
    }
}
