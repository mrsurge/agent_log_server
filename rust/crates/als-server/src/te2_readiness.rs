use crate::config::framework_url_from_env;
use als_dto::APP_ID;
use std::{
    env,
    sync::atomic::{AtomicBool, Ordering},
};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::TcpStream,
};
use tracing::{info, warn};

static READY_POST_STARTED: AtomicBool = AtomicBool::new(false);

const READY_BODY: &str = r#"{"status":"ready"}"#;

#[derive(Debug, Eq, PartialEq)]
struct ReadinessTarget {
    app_id: String,
    host: String,
    port: u16,
    path: String,
    host_header: String,
}

pub fn post_ready_after_server_started() {
    let Some(target) = ReadinessTarget::from_env() else {
        return;
    };
    if READY_POST_STARTED
        .compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
        .is_err()
    {
        return;
    }
    tokio::spawn(async move {
        match post_ready(target).await {
            Ok(()) => info!("posted ALS-RS semantic readiness to TE2"),
            Err(error) => {
                READY_POST_STARTED.store(false, Ordering::SeqCst);
                warn!(%error, "failed to post ALS-RS semantic readiness to TE2");
            }
        }
    });
}

async fn post_ready(target: ReadinessTarget) -> Result<(), String> {
    let mut stream = TcpStream::connect(format!("{}:{}", target.host, target.port))
        .await
        .map_err(|error| format!("connect failed: {error}"))?;
    let request = format!(
        "POST {} HTTP/1.1\r\nHost: {}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
        target.path,
        target.host_header,
        READY_BODY.len(),
        READY_BODY,
    );
    stream
        .write_all(request.as_bytes())
        .await
        .map_err(|error| format!("write failed: {error}"))?;
    let mut response = Vec::new();
    stream
        .read_to_end(&mut response)
        .await
        .map_err(|error| format!("read failed: {error}"))?;
    let text = String::from_utf8_lossy(&response);
    let status_line = text.lines().next().unwrap_or_default();
    let status = status_line
        .split_whitespace()
        .nth(1)
        .and_then(|value| value.parse::<u16>().ok())
        .unwrap_or(0);
    if (200..300).contains(&status) {
        Ok(())
    } else {
        Err(format!(
            "unexpected readiness response status {} ({})",
            status, status_line
        ))
    }
}

impl ReadinessTarget {
    fn from_env() -> Option<Self> {
        let app_id = env::var("TE_APP_ID")
            .ok()
            .map(|value| value.trim().to_owned())
            .filter(|value| !value.is_empty())?;
        let framework_url = framework_url_from_env();
        Self::from_base_url(&framework_url, &app_id).or_else(|| {
            warn!(%framework_url, "invalid TE framework URL for readiness POST");
            None
        })
    }

    fn from_base_url(raw_base_url: &str, app_id: &str) -> Option<Self> {
        let app_id = if app_id.trim().is_empty() {
            APP_ID.to_owned()
        } else {
            app_id.trim().to_owned()
        };
        let mut raw = raw_base_url.trim();
        if raw.is_empty() {
            return None;
        }
        let normalized;
        if !raw.starts_with("http://") {
            normalized = format!("http://{raw}");
            raw = &normalized;
        }
        let rest = raw.strip_prefix("http://")?;
        let (authority, base_path) = rest.split_once('/').unwrap_or((rest, ""));
        let (host, port) = parse_authority(authority)?;
        let path_prefix = normalize_path_prefix(base_path);
        let endpoint_path = format!(
            "{}/api/apps/{}/readiness",
            path_prefix,
            percent_encode_path_segment(&app_id)
        )
        .replace("//", "/");
        Some(Self {
            app_id,
            host: host.to_owned(),
            port,
            path: endpoint_path,
            host_header: if authority.contains(':') {
                authority.to_owned()
            } else {
                format!("{authority}:{port}")
            },
        })
    }
}

fn parse_authority(authority: &str) -> Option<(&str, u16)> {
    let authority = authority.trim();
    if authority.is_empty() || authority.contains('@') {
        return None;
    }
    let (host, port) = match authority.rsplit_once(':') {
        Some((host, port)) if !host.is_empty() => (host, port.parse::<u16>().ok()?),
        _ => (authority, 80),
    };
    if host.is_empty() {
        return None;
    }
    Some((host, port))
}

fn normalize_path_prefix(path: &str) -> String {
    let trimmed = path.trim_matches('/');
    if trimmed.is_empty() {
        String::new()
    } else {
        format!("/{trimmed}")
    }
}

fn percent_encode_path_segment(value: &str) -> String {
    let mut encoded = String::new();
    for byte in value.as_bytes() {
        let ch = *byte as char;
        if ch.is_ascii_alphanumeric() || matches!(ch, '-' | '_' | '.') {
            encoded.push(ch);
        } else {
            encoded.push_str(&format!("%{byte:02X}"));
        }
    }
    encoded
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn builds_default_te2_readiness_target() {
        let target = ReadinessTarget::from_base_url("http://127.0.0.1:8089", "als-rs").unwrap();
        assert_eq!(target.host, "127.0.0.1");
        assert_eq!(target.port, 8089);
        assert_eq!(target.path, "/api/apps/als-rs/readiness");
        assert_eq!(target.host_header, "127.0.0.1:8089");
    }

    #[test]
    fn preserves_framework_path_prefix() {
        let target =
            ReadinessTarget::from_base_url("http://127.0.0.1:8089/root/", "als rs").unwrap();
        assert_eq!(target.path, "/root/api/apps/als%20rs/readiness");
    }
}
