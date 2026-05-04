use axum::{
    Json, Router,
    http::header::CONTENT_TYPE,
    response::{Html, IntoResponse},
    routing::get,
};
use serde_json::json;
use std::path::Path;
use tower_http::services::ServeDir;

use crate::state::AppState;

pub fn routes(static_dir: &Path) -> Router<AppState> {
    Router::new()
        .route("/", get(index))
        .route("/manifest.json", get(manifest))
        .route("/sw.js", get(service_worker))
        .nest_service("/static", ServeDir::new(static_dir))
}

async fn index() -> Html<&'static str> {
    Html(include_str!("index.html"))
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
