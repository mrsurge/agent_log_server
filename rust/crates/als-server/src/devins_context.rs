use als_adapter_protocol::{DeveloperInstructionsContext, JsonMap};
use anyhow::{Context, Result, anyhow, bail};
use serde_json::Value;
use std::{
    env, fs,
    path::{Path, PathBuf},
    process::Command,
};

const REPO_MEMORY_FILENAME: &str = ".repo_memory.md";
const TEMPLATE_FILENAME: &str = "DEVELOPER_MESSAGE_TEMPLATE.md";

pub fn build_devins_context(
    settings: Option<&JsonMap>,
    cwd: Option<&str>,
) -> Result<DeveloperInstructionsContext> {
    let user = string_from_settings(settings, "developer_instructions");
    let te2_enabled = bool_from_settings(settings, "te2_mcp_integration");
    let logical_cwd =
        logical_cwd_path(cwd.or_else(|| settings.and_then(|map| string_ref(map, "cwd"))));
    let snapshot = logical_cwd
        .as_ref()
        .map(load_repo_memory_snapshot)
        .transpose()?
        .unwrap_or_default();
    let (template_path, template_text) = if te2_enabled {
        let path = template_path()?;
        let text = fs::read_to_string(&path)
            .with_context(|| {
                format!(
                    "failed to read TE2 developer message template {}",
                    path.display()
                )
            })?
            .trim()
            .to_owned();
        if text.is_empty() {
            bail!(
                "TE2 developer message template is empty: {}",
                path.display()
            );
        }
        (Some(path), Some(text))
    } else {
        (None, None)
    };
    let effective = build_effective_text(
        user.as_deref(),
        te2_enabled,
        template_text.as_deref(),
        snapshot.content.as_deref(),
    );

    Ok(DeveloperInstructionsContext {
        effective,
        user,
        te2_enabled,
        cwd: logical_cwd,
        template_path,
        repo_root: snapshot.repo_root,
        repo_memory_path: snapshot.repo_memory_path,
        repo_memory_exists: snapshot.exists,
        repo_memory_truncated: snapshot.truncated,
    })
}

fn build_effective_text(
    user: Option<&str>,
    te2_enabled: bool,
    template: Option<&str>,
    repo_memory: Option<&str>,
) -> Option<String> {
    let user_text = user.map(str::trim).filter(|value| !value.is_empty());
    if !te2_enabled {
        return user_text.map(ToOwned::to_owned);
    }

    let template_text = template.map(str::trim).filter(|value| !value.is_empty());
    let developer_text = match (template_text, user_text) {
        (Some(template), Some(user)) if user.contains(template) => Some(user.to_owned()),
        (Some(template), Some(user)) => Some(format!("{template}\n\n{user}")),
        (Some(template), None) => Some(template.to_owned()),
        (None, Some(user)) => Some(user.to_owned()),
        (None, None) => None,
    };

    let memory_text = repo_memory.map(str::trim).filter(|value| !value.is_empty());
    match (developer_text, memory_text) {
        (Some(developer), Some(memory)) if developer.contains(memory) => Some(developer),
        (Some(developer), Some(memory)) => Some(format!("{developer}\n\n{memory}")),
        (None, Some(memory)) => Some(memory.to_owned()),
        (developer, None) => developer,
    }
}

#[derive(Default)]
struct RepoMemorySnapshot {
    repo_root: Option<PathBuf>,
    repo_memory_path: Option<PathBuf>,
    exists: bool,
    truncated: bool,
    content: Option<String>,
}

fn load_repo_memory_snapshot(start: &PathBuf) -> Result<RepoMemorySnapshot> {
    let repo_root = detect_repo_memory_root(start);
    let memory_path = repo_root.join(REPO_MEMORY_FILENAME);
    if !memory_path.is_file() {
        return Ok(RepoMemorySnapshot {
            repo_root: Some(repo_root),
            repo_memory_path: Some(memory_path),
            ..RepoMemorySnapshot::default()
        });
    }

    let text = fs::read_to_string(&memory_path)
        .with_context(|| format!("failed to read repo memory {}", memory_path.display()))?
        .trim()
        .to_owned();
    Ok(RepoMemorySnapshot {
        repo_root: Some(repo_root),
        repo_memory_path: Some(memory_path),
        exists: true,
        truncated: false,
        content: (!text.is_empty()).then_some(text),
    })
}

fn detect_repo_memory_root(start: &Path) -> PathBuf {
    if let Ok(output) = Command::new("git")
        .arg("-C")
        .arg(start)
        .arg("rev-parse")
        .arg("--show-toplevel")
        .output()
    {
        if output.status.success() {
            let root = String::from_utf8_lossy(&output.stdout).trim().to_owned();
            if !root.is_empty() {
                return PathBuf::from(root);
            }
        }
    }

    let mut current = start.to_path_buf();
    loop {
        if current.join(".agent-pty.toml").exists() || current.join(REPO_MEMORY_FILENAME).exists() {
            return current;
        }
        let Some(parent) = current.parent() else {
            break;
        };
        if parent == current {
            break;
        }
        current = parent.to_path_buf();
    }
    start.to_path_buf()
}

fn logical_cwd_path(cwd: Option<&str>) -> Option<PathBuf> {
    let path = cwd.map(str::trim).filter(|value| !value.is_empty())?;
    let expanded = if let Some(rest) = path.strip_prefix("~/") {
        home_dir().join(rest)
    } else if path == "~" {
        home_dir()
    } else {
        PathBuf::from(path)
    };
    if expanded.is_file() {
        expanded.parent().map(Path::to_path_buf)
    } else {
        Some(expanded)
    }
}

fn template_path() -> Result<PathBuf> {
    if let Some(raw) = env::var_os("TE2_DEVELOPER_MESSAGE_TEMPLATE_PATH") {
        let path = PathBuf::from(raw);
        if !path.as_os_str().is_empty() {
            if !path.is_file() {
                return Err(anyhow!(
                    "TE2 developer message template not found: {}",
                    path.display()
                ));
            }
            return Ok(path);
        }
    }

    template_candidates()
        .into_iter()
        .find(|path| path.is_file())
        .ok_or_else(|| anyhow!("TE2 developer message template not found"))
}

fn template_candidates() -> Vec<PathBuf> {
    let root = compile_time_repo_root();
    vec![
        root.join("als_deprecated").join(TEMPLATE_FILENAME),
        root.join(TEMPLATE_FILENAME),
        root.parent()
            .map(|parent| parent.join("als_deprecated").join(TEMPLATE_FILENAME))
            .unwrap_or_else(|| root.join("als_deprecated").join(TEMPLATE_FILENAME)),
    ]
}

fn compile_time_repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..")
        .join("..")
}

fn home_dir() -> PathBuf {
    env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
}

fn string_from_settings(settings: Option<&JsonMap>, key: &str) -> Option<String> {
    settings
        .and_then(|map| string_ref(map, key))
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(ToOwned::to_owned)
}

fn string_ref<'a>(map: &'a JsonMap, key: &str) -> Option<&'a str> {
    map.get(key).and_then(Value::as_str)
}

fn bool_from_settings(settings: Option<&JsonMap>, key: &str) -> bool {
    settings
        .and_then(|map| map.get(key))
        .and_then(Value::as_bool)
        .unwrap_or(false)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::{
        fs,
        sync::{Mutex, OnceLock},
        time::{SystemTime, UNIX_EPOCH},
    };

    fn env_lock() -> &'static Mutex<()> {
        static LOCK: OnceLock<Mutex<()>> = OnceLock::new();
        LOCK.get_or_init(|| Mutex::new(()))
    }

    fn test_root(name: &str) -> PathBuf {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        env::temp_dir().join(format!("als-rs-devins-{name}-{nonce}"))
    }

    #[test]
    fn te2_disabled_returns_raw_user_instructions() {
        let mut settings = JsonMap::new();
        settings.insert("developer_instructions".to_owned(), json!("  hello  "));
        let context = build_devins_context(Some(&settings), None).unwrap();
        assert_eq!(context.effective.as_deref(), Some("hello"));
        assert!(!context.te2_enabled);
    }

    #[test]
    fn te2_enabled_concatenates_template_user_and_repo_memory() {
        let _guard = env_lock().lock().unwrap();
        let root = test_root("concat");
        let nested = root.join("nested");
        fs::create_dir_all(&nested).unwrap();
        fs::write(root.join(".agent-pty.toml"), "").unwrap();
        fs::write(root.join(REPO_MEMORY_FILENAME), "Repo memory").unwrap();
        let template = root.join("template.md");
        fs::write(&template, "Template").unwrap();
        unsafe {
            env::set_var("TE2_DEVELOPER_MESSAGE_TEMPLATE_PATH", &template);
        }

        let mut settings = JsonMap::new();
        settings.insert("developer_instructions".to_owned(), json!("User devins"));
        settings.insert("te2_mcp_integration".to_owned(), json!(true));
        let context = build_devins_context(Some(&settings), nested.to_str()).unwrap();

        assert_eq!(
            context.effective.as_deref(),
            Some("Template\n\nUser devins\n\nRepo memory")
        );
        assert_eq!(context.repo_root.as_deref(), Some(root.as_path()));
        assert_eq!(
            context.repo_memory_path.as_deref(),
            Some(root.join(REPO_MEMORY_FILENAME).as_path())
        );
        assert!(context.repo_memory_exists);
        unsafe {
            env::remove_var("TE2_DEVELOPER_MESSAGE_TEMPLATE_PATH");
        }
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn deduplicates_existing_template_and_repo_memory() {
        assert_eq!(
            build_effective_text(
                Some("Template\n\nUser\n\nRepo memory"),
                true,
                Some("Template"),
                Some("Repo memory")
            )
            .as_deref(),
            Some("Template\n\nUser\n\nRepo memory")
        );
    }

    #[test]
    fn file_cwd_uses_parent_for_repo_memory_lookup() {
        let root = test_root("file-cwd");
        let nested = root.join("nested");
        fs::create_dir_all(&nested).unwrap();
        let file = nested.join("file.txt");
        fs::write(&file, "").unwrap();
        fs::write(root.join(REPO_MEMORY_FILENAME), "Memory").unwrap();
        let logical = logical_cwd_path(file.to_str());
        assert_eq!(logical.as_deref(), Some(nested.as_path()));
        let snapshot = load_repo_memory_snapshot(&logical.unwrap()).unwrap();
        assert_eq!(snapshot.content.as_deref(), Some("Memory"));
        fs::remove_dir_all(root).unwrap();
    }
}
