use crate::conversation_store::ConversationStore;
use anyhow::{Context, Result, anyhow, bail};
use git2::Repository;
use serde::Serialize;
use serde_json::{Map, Value, json};
use std::{
    borrow::Cow,
    collections::HashMap,
    hash::{Hash, Hasher},
    io::Write,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::{Arc, Mutex},
    time::{SystemTime, UNIX_EPOCH},
};

#[derive(Clone, Debug, Serialize)]
pub struct TrackedAgentDiff {
    pub id: String,
    pub conversation_id: String,
    pub path: Option<String>,
    pub abs: Option<String>,
    pub rel: Option<String>,
    pub line: u64,
    pub column: u64,
    pub source: String,
    pub created_at: String,
    pub repo_root: Option<String>,
    pub diff_text: String,
    pub diff_bytes: usize,
    pub additions: usize,
    pub deletions: usize,
}

#[derive(Clone, Default)]
pub struct AgentEditLedger {
    inner: Arc<Mutex<HashMap<String, Vec<TrackedAgentDiff>>>>,
}

impl AgentEditLedger {
    pub fn record_live_diff(
        &self,
        conversations: &ConversationStore,
        conversation_id: &str,
        event: &Value,
    ) -> Result<Option<TrackedAgentDiff>> {
        let Some(object) = event.as_object() else {
            return Ok(None);
        };
        let Some(meta) = conversations.load_meta_if_exists(conversation_id)? else {
            return Ok(None);
        };
        if meta.settings.get("trackEdits").and_then(Value::as_bool) != Some(true) {
            return Ok(None);
        }
        let Some(diff_text) = string_from_object(object, &["text", "diff"]) else {
            return Ok(None);
        };
        if diff_text.trim().is_empty() {
            return Ok(None);
        }

        let cwd = meta
            .settings
            .get("cwd")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .map(PathBuf::from)
            .or_else(|| {
                meta.cwd
                    .as_deref()
                    .map(str::trim)
                    .filter(|value| !value.is_empty())
                    .map(PathBuf::from)
            });
        let repo_root = cwd.as_deref().and_then(repo_root_from_path);
        let explicit_path = string_from_object(object, &["path", "file", "abs", "rel"]);
        let diff_path = explicit_path
            .clone()
            .or_else(|| extract_path_from_diff(&diff_text));
        let (abs, rel) = normalize_paths(repo_root.as_deref(), diff_path.as_deref());
        let line = extract_line_from_diff(&diff_text).unwrap_or(1);
        let id = string_from_object(object, &["id", "card_id", "item_id", "request_id"])
            .unwrap_or_else(|| {
                generated_diff_id(conversation_id, diff_path.as_deref(), &diff_text)
            });
        let (additions, deletions) = count_diff_lines(&diff_text);
        let entry = TrackedAgentDiff {
            id,
            conversation_id: conversation_id.to_owned(),
            path: diff_path,
            abs,
            rel,
            line,
            column: 1,
            source: string_from_object(object, &["source"])
                .unwrap_or_else(|| "appserver_diff".to_owned()),
            created_at: string_from_object(object, &["created_at"]).unwrap_or_else(utc_ts),
            repo_root: repo_root.as_deref().map(path_to_string),
            diff_bytes: diff_text.len(),
            diff_text,
            additions,
            deletions,
        };

        let mut guard = self
            .inner
            .lock()
            .map_err(|_| anyhow!("agent edit ledger lock poisoned"))?;
        let entries = guard.entry(conversation_id.to_owned()).or_default();
        if entries.iter().any(|existing| existing.id == entry.id) {
            return Ok(None);
        }
        entries.push(entry.clone());
        Ok(Some(entry))
    }

    pub fn list(&self, conversation_id: &str) -> Result<Vec<TrackedAgentDiff>> {
        let guard = self
            .inner
            .lock()
            .map_err(|_| anyhow!("agent edit ledger lock poisoned"))?;
        Ok(guard.get(conversation_id).cloned().unwrap_or_default())
    }

    pub fn accept(&self, conversation_id: &str, diff_id: &str) -> Result<Option<TrackedAgentDiff>> {
        self.remove(conversation_id, diff_id)
    }

    pub fn get(&self, conversation_id: &str, diff_id: &str) -> Result<Option<TrackedAgentDiff>> {
        let guard = self
            .inner
            .lock()
            .map_err(|_| anyhow!("agent edit ledger lock poisoned"))?;
        Ok(guard
            .get(conversation_id)
            .and_then(|entries| entries.iter().find(|entry| entry.id == diff_id).cloned()))
    }

    pub fn remove(&self, conversation_id: &str, diff_id: &str) -> Result<Option<TrackedAgentDiff>> {
        let mut guard = self
            .inner
            .lock()
            .map_err(|_| anyhow!("agent edit ledger lock poisoned"))?;
        let Some(entries) = guard.get_mut(conversation_id) else {
            return Ok(None);
        };
        let Some(index) = entries.iter().position(|entry| entry.id == diff_id) else {
            return Ok(None);
        };
        let removed = entries.remove(index);
        if entries.is_empty() {
            guard.remove(conversation_id);
        }
        Ok(Some(removed))
    }
}

impl TrackedAgentDiff {
    pub fn sidebar_payload(&self) -> Map<String, Value> {
        let mut payload = Map::new();
        if let Some(path) = self.abs.as_ref().or(self.path.as_ref()) {
            payload.insert("path".to_owned(), Value::String(path.clone()));
        }
        if let Some(abs) = self.abs.as_ref() {
            payload.insert("abs".to_owned(), Value::String(abs.clone()));
        }
        if let Some(rel) = self.rel.as_ref() {
            payload.insert("rel".to_owned(), Value::String(rel.clone()));
        }
        payload.insert("line".to_owned(), Value::Number(self.line.into()));
        payload.insert("column".to_owned(), Value::Number(self.column.into()));
        payload.insert("source".to_owned(), Value::String(self.source.clone()));
        payload.insert(
            "conversation_id".to_owned(),
            Value::String(self.conversation_id.clone()),
        );
        payload.insert("diff_id".to_owned(), Value::String(self.id.clone()));
        payload
    }
}

pub fn apply_reverse_patch(repo_root: &Path, diff_text: &str) -> Result<()> {
    let diff_text = normalized_patch_text(diff_text);
    run_git_apply(repo_root, diff_text.as_ref(), true)?;
    run_git_apply(repo_root, diff_text.as_ref(), false)
}

fn normalized_patch_text(diff_text: &str) -> Cow<'_, str> {
    if diff_text.ends_with('\n') {
        Cow::Borrowed(diff_text)
    } else {
        Cow::Owned(format!("{diff_text}\n"))
    }
}

fn run_git_apply(repo_root: &Path, diff_text: &str, check: bool) -> Result<()> {
    let mut command = Command::new("git");
    command
        .arg("apply")
        .arg("--reverse")
        .current_dir(repo_root)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    if check {
        command.arg("--check");
    }
    let mut child = command
        .spawn()
        .with_context(|| format!("failed to start git apply in {}", repo_root.display()))?;
    let mut stdin = child
        .stdin
        .take()
        .ok_or_else(|| anyhow!("git apply stdin unavailable"))?;
    stdin.write_all(diff_text.as_bytes())?;
    drop(stdin);
    let output = child.wait_with_output()?;
    if output.status.success() {
        return Ok(());
    }
    let stderr = String::from_utf8_lossy(&output.stderr);
    let stdout = String::from_utf8_lossy(&output.stdout);
    let detail = stderr.trim();
    if !detail.is_empty() {
        bail!("{detail}");
    }
    let detail = stdout.trim();
    if !detail.is_empty() {
        bail!("{detail}");
    }
    bail!("git apply --reverse failed");
}

fn repo_root_from_path(path: &Path) -> Option<PathBuf> {
    Repository::discover(path)
        .ok()
        .and_then(|repo| repo.workdir().map(Path::to_path_buf))
}

fn normalize_paths(
    repo_root: Option<&Path>,
    path: Option<&str>,
) -> (Option<String>, Option<String>) {
    let Some(path) = path.map(str::trim).filter(|value| !value.is_empty()) else {
        return (None, None);
    };
    let path_buf = PathBuf::from(path);
    if path_buf.is_absolute() {
        let abs = path_to_string(&path_buf);
        let rel = repo_root
            .and_then(|root| path_buf.strip_prefix(root).ok())
            .map(path_to_string);
        return (Some(abs), rel);
    }
    let rel = path.trim_start_matches("./").to_owned();
    let abs = repo_root.map(|root| path_to_string(&root.join(&rel)));
    (abs, Some(rel))
}

fn string_from_object(object: &Map<String, Value>, keys: &[&str]) -> Option<String> {
    keys.iter().find_map(|key| {
        object
            .get(*key)
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .map(ToOwned::to_owned)
    })
}

fn extract_path_from_diff(diff_text: &str) -> Option<String> {
    for line in diff_text.lines() {
        if let Some(rest) = line.strip_prefix("+++ b/") {
            let path = rest.trim();
            if path != "/dev/null" && !path.is_empty() {
                return Some(path.to_owned());
            }
        }
    }
    for line in diff_text.lines() {
        if let Some(rest) = line.strip_prefix("diff --git ") {
            let mut parts = rest.split_whitespace();
            let _old = parts.next();
            if let Some(new_path) = parts.next().and_then(|value| value.strip_prefix("b/")) {
                if !new_path.trim().is_empty() {
                    return Some(new_path.trim().to_owned());
                }
            }
        }
    }
    None
}

fn extract_line_from_diff(diff_text: &str) -> Option<u64> {
    for line in diff_text.lines() {
        if !line.starts_with("@@") {
            continue;
        }
        for part in line.split_whitespace() {
            let Some(rest) = part.strip_prefix('+') else {
                continue;
            };
            let number = rest.split(',').next().unwrap_or(rest);
            if let Ok(line_no) = number.parse::<u64>() {
                return Some(line_no.max(1));
            }
        }
    }
    None
}

fn count_diff_lines(diff_text: &str) -> (usize, usize) {
    let mut additions = 0usize;
    let mut deletions = 0usize;
    for line in diff_text.lines() {
        if line.starts_with("+++") || line.starts_with("---") {
            continue;
        }
        if line.starts_with('+') {
            additions += 1;
        } else if line.starts_with('-') {
            deletions += 1;
        }
    }
    (additions, deletions)
}

fn generated_diff_id(conversation_id: &str, path: Option<&str>, diff_text: &str) -> String {
    let mut hasher = std::collections::hash_map::DefaultHasher::new();
    conversation_id.hash(&mut hasher);
    path.hash(&mut hasher);
    diff_text.hash(&mut hasher);
    unix_millis().hash(&mut hasher);
    format!("agent_diff_{:x}", hasher.finish())
}

fn utc_ts() -> String {
    format!("unix_ms:{}", unix_millis())
}

fn unix_millis() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|value| value.as_millis())
        .unwrap_or_default()
}

fn path_to_string(path: &Path) -> String {
    path.to_string_lossy().into_owned()
}

pub fn agent_diffs_json(entries: Vec<TrackedAgentDiff>) -> Value {
    json!(entries)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn extracts_first_new_line_from_hunk() {
        assert_eq!(
            extract_line_from_diff("@@ -10,6 +42,7 @@ fn main() {\n+added"),
            Some(42)
        );
    }

    #[test]
    fn extracts_path_from_unified_diff() {
        assert_eq!(
            extract_path_from_diff(
                "diff --git a/src/lib.rs b/src/lib.rs\n--- a/src/lib.rs\n+++ b/src/lib.rs\n@@ -1 +1 @@"
            ),
            Some("src/lib.rs".to_owned())
        );
    }

    #[test]
    fn counts_non_header_diff_lines() {
        assert_eq!(
            count_diff_lines("--- a/file\n+++ b/file\n-old\n+new\n context"),
            (1, 1)
        );
    }

    #[test]
    fn normalizes_patch_text_with_trailing_newline() {
        assert_eq!(
            normalized_patch_text("diff --git a/file b/file").as_ref(),
            "diff --git a/file b/file\n"
        );
        assert!(matches!(
            normalized_patch_text("diff --git a/file b/file\n"),
            Cow::Borrowed(_)
        ));
    }
}
