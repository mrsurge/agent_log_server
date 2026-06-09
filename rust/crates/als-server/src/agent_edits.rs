use crate::conversation_store::ConversationStore;
use anyhow::{Result, anyhow};
use git2::Repository;
use serde::Serialize;
use serde_json::{Map, Value, json};
use std::{
    collections::HashMap,
    hash::{Hash, Hasher},
    path::{Path, PathBuf},
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

    pub fn list_newest_first(&self, conversation_id: &str) -> Result<Vec<TrackedAgentDiff>> {
        let mut entries = self.list(conversation_id)?;
        entries.reverse();
        Ok(entries)
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

    pub fn inline_publish_payload(&self) -> Option<Map<String, Value>> {
        let uri = self.file_uri()?;
        let mut edit = Map::new();
        edit.insert("editId".to_owned(), Value::String(self.id.clone()));
        edit.insert("revision".to_owned(), Value::Number(1.into()));
        edit.insert("state".to_owned(), Value::String("pending".to_owned()));
        edit.insert("uri".to_owned(), Value::String(uri.clone()));
        if let Some(path) = self.abs.as_ref().or(self.path.as_ref()) {
            edit.insert("path".to_owned(), Value::String(path.clone()));
        }
        if let Some(rel) = self.rel.as_ref() {
            edit.insert("rel".to_owned(), Value::String(rel.clone()));
        }
        edit.insert("label".to_owned(), Value::String("Agent edit".to_owned()));
        edit.insert(
            "description".to_owned(),
            Value::String(format!(
                "{} addition{}, {} deletion{}",
                self.additions,
                plural(self.additions),
                self.deletions,
                plural(self.deletions)
            )),
        );
        edit.insert("source".to_owned(), Value::String(self.source.clone()));
        edit.insert("line".to_owned(), Value::Number(self.line.into()));
        edit.insert(
            "modifiedRange".to_owned(),
            inline_range(self.line, self.line),
        );
        edit.insert(
            "hunks".to_owned(),
            Value::Array(inline_hunks_from_diff(
                &self.id,
                &self.diff_text,
                self.line,
                self.additions,
                self.deletions,
            )),
        );

        let mut payload = Map::new();
        payload.insert(
            "conversationId".to_owned(),
            Value::String(self.conversation_id.clone()),
        );
        if let Some(project_path) = self.repo_root.as_ref() {
            payload.insert(
                "projectPath".to_owned(),
                Value::String(project_path.clone()),
            );
        }
        payload.insert("source".to_owned(), Value::String(self.source.clone()));
        payload.insert("uri".to_owned(), Value::String(uri));
        payload.insert("edits".to_owned(), Value::Array(vec![Value::Object(edit)]));
        Some(payload)
    }

    pub fn inline_document_state_params(&self) -> Option<Map<String, Value>> {
        let uri = self.file_uri()?;
        let mut params = Map::new();
        params.insert("uri".to_owned(), Value::String(uri));
        params.insert(
            "conversationId".to_owned(),
            Value::String(self.conversation_id.clone()),
        );
        if let Some(project_path) = self.repo_root.as_ref() {
            params.insert(
                "projectPath".to_owned(),
                Value::String(project_path.clone()),
            );
        }
        Some(params)
    }

    pub fn inline_clear_payload(&self) -> Option<Map<String, Value>> {
        let uri = self.file_uri()?;
        let mut params = Map::new();
        params.insert("uri".to_owned(), Value::String(uri));
        params.insert("editId".to_owned(), Value::String(self.id.clone()));
        params.insert("diffId".to_owned(), Value::String(self.id.clone()));
        params.insert(
            "conversationId".to_owned(),
            Value::String(self.conversation_id.clone()),
        );
        if let Some(project_path) = self.repo_root.as_ref() {
            params.insert(
                "projectPath".to_owned(),
                Value::String(project_path.clone()),
            );
        }
        params.insert("source".to_owned(), Value::String(self.source.clone()));
        Some(params)
    }

    fn file_uri(&self) -> Option<String> {
        self.abs
            .as_ref()
            .or(self.path.as_ref())
            .map(|path| format!("file://{}", path.replace(' ', "%20")))
    }
}

pub fn apply_reverse_patch(repo_root: &Path, entry: &TrackedAgentDiff) -> Result<()> {
    let path_hint = entry
        .rel
        .as_deref()
        .or(entry.abs.as_deref())
        .or(entry.path.as_deref());
    crate::reverse_patch::apply_reverse_patch(repo_root, path_hint, &entry.diff_text)
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

fn inline_hunks_from_diff(
    edit_id: &str,
    diff_text: &str,
    fallback_line: u64,
    additions: usize,
    deletions: usize,
) -> Vec<Value> {
    let mut hunks = Vec::new();
    let mut hunk_number = 0usize;
    let mut original_line = 1u64;
    let mut modified_line = 1u64;
    let mut current_summary = "Agent edit hunk".to_owned();
    let mut current_block: Option<InlineHunkBlock> = None;

    let flush_block = |hunks: &mut Vec<Value>,
                       hunk_number: &mut usize,
                       block: &mut Option<InlineHunkBlock>,
                       summary: &str| {
        let Some(block) = block.take() else {
            return;
        };
        if block.original_lines.is_empty() && block.modified_lines.is_empty() {
            return;
        }
        *hunk_number += 1;
        hunks.push(Value::Object(inline_hunk_from_block(
            edit_id,
            *hunk_number,
            block,
            summary,
        )));
    };

    for line in diff_text.lines() {
        if line.starts_with("@@") {
            flush_block(
                &mut hunks,
                &mut hunk_number,
                &mut current_block,
                &current_summary,
            );
            let Some((original_start, _original_count, modified_start, _modified_count)) =
                parse_hunk_header(line)
            else {
                continue;
            };
            original_line = original_start;
            modified_line = modified_start;
            current_summary = hunk_summary(line);
            continue;
        }

        if line.starts_with("diff --git ")
            || line.starts_with("--- ")
            || line.starts_with("+++ ")
            || line.starts_with("index ")
            || line.starts_with("\\ No newline at end of file")
        {
            continue;
        }

        if let Some(text) = line.strip_prefix('-') {
            let block = current_block.get_or_insert_with(|| InlineHunkBlock {
                original_start: original_line,
                modified_start: modified_line,
                original_lines: Vec::new(),
                modified_lines: Vec::new(),
            });
            block.original_lines.push(text.to_owned());
            original_line = original_line.saturating_add(1);
            continue;
        }

        if let Some(text) = line.strip_prefix('+') {
            let block = current_block.get_or_insert_with(|| InlineHunkBlock {
                original_start: original_line,
                modified_start: modified_line,
                original_lines: Vec::new(),
                modified_lines: Vec::new(),
            });
            block.modified_lines.push(text.to_owned());
            modified_line = modified_line.saturating_add(1);
            continue;
        }

        flush_block(
            &mut hunks,
            &mut hunk_number,
            &mut current_block,
            &current_summary,
        );
        if line.starts_with(' ') {
            original_line = original_line.saturating_add(1);
            modified_line = modified_line.saturating_add(1);
        }
    }
    flush_block(
        &mut hunks,
        &mut hunk_number,
        &mut current_block,
        &current_summary,
    );

    if hunks.is_empty() {
        let mut hunk = Map::new();
        hunk.insert(
            "hunkId".to_owned(),
            Value::String(format!("{edit_id}:hunk-1")),
        );
        hunk.insert("kind".to_owned(), Value::String("modified".to_owned()));
        hunk.insert("state".to_owned(), Value::String("pending".to_owned()));
        hunk.insert("originalLines".to_owned(), Value::Array(Vec::new()));
        hunk.insert("modifiedLines".to_owned(), Value::Array(Vec::new()));
        hunk.insert(
            "modifiedRange".to_owned(),
            inline_range(fallback_line, fallback_line),
        );
        hunk.insert(
            "summary".to_owned(),
            Value::String(format!(
                "{} addition{}, {} deletion{}",
                additions,
                plural(additions),
                deletions,
                plural(deletions)
            )),
        );
        hunks.push(Value::Object(hunk));
    }
    hunks
}

struct InlineHunkBlock {
    original_start: u64,
    modified_start: u64,
    original_lines: Vec<String>,
    modified_lines: Vec<String>,
}

fn inline_hunk_from_block(
    edit_id: &str,
    hunk_number: usize,
    block: InlineHunkBlock,
    summary: &str,
) -> Map<String, Value> {
    let InlineHunkBlock {
        original_start,
        modified_start,
        original_lines,
        modified_lines,
    } = block;
    let original_len = original_lines.len() as u64;
    let modified_len = modified_lines.len() as u64;
    let original_line_values = original_lines.into_iter().map(Value::String).collect();
    let modified_line_values = modified_lines.into_iter().map(Value::String).collect();
    let mut hunk = Map::new();
    hunk.insert(
        "hunkId".to_owned(),
        Value::String(format!("{edit_id}:hunk-{hunk_number}")),
    );
    hunk.insert(
        "kind".to_owned(),
        Value::String(
            match (original_len > 0, modified_len > 0) {
                (true, true) => "modified",
                (true, false) => "deleted",
                (false, true) => "added",
                (false, false) => "modified",
            }
            .to_owned(),
        ),
    );
    hunk.insert("state".to_owned(), Value::String("pending".to_owned()));
    if original_len > 0 {
        hunk.insert(
            "originalRange".to_owned(),
            inline_range(original_start, range_end_line(original_start, original_len)),
        );
    } else {
        hunk.insert(
            "originalRange".to_owned(),
            inline_range(original_start, original_start),
        );
    }
    if modified_len > 0 {
        hunk.insert(
            "modifiedRange".to_owned(),
            inline_range(modified_start, range_end_line(modified_start, modified_len)),
        );
    } else {
        hunk.insert(
            "modifiedRange".to_owned(),
            inline_range(modified_start, modified_start),
        );
    }
    hunk.insert(
        "originalLines".to_owned(),
        Value::Array(original_line_values),
    );
    hunk.insert(
        "modifiedLines".to_owned(),
        Value::Array(modified_line_values),
    );
    hunk.insert(
        "summary".to_owned(),
        Value::String(if summary.trim().is_empty() {
            "Agent edit hunk".to_owned()
        } else {
            summary.to_owned()
        }),
    );
    hunk
}

fn parse_hunk_header(line: &str) -> Option<(u64, u64, u64, u64)> {
    let mut original = None;
    let mut modified = None;
    for part in line.split_whitespace() {
        if let Some(rest) = part.strip_prefix('-') {
            original = parse_range_part(rest);
        } else if let Some(rest) = part.strip_prefix('+') {
            modified = parse_range_part(rest);
        }
        if original.is_some() && modified.is_some() {
            break;
        }
    }
    let (original_start, original_count) = original?;
    let (modified_start, modified_count) = modified?;
    Some((
        original_start,
        original_count,
        modified_start,
        modified_count,
    ))
}

fn parse_range_part(value: &str) -> Option<(u64, u64)> {
    let mut parts = value.split(',');
    let start = parts.next()?.parse::<u64>().ok()?.max(1);
    let count = parts
        .next()
        .and_then(|value| value.parse::<u64>().ok())
        .unwrap_or(1);
    Some((start, count))
}

fn range_end_line(start: u64, count: u64) -> u64 {
    if count == 0 {
        start
    } else {
        start.saturating_add(count.saturating_sub(1))
    }
}

fn inline_range(start_line: u64, end_line: u64) -> Value {
    json!({
        "startLineNumber": start_line,
        "startColumn": 1,
        "endLineNumber": end_line.max(start_line),
        "endColumn": 1,
    })
}

fn hunk_summary(header: &str) -> String {
    header
        .split("@@")
        .nth(2)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .unwrap_or("Agent edit hunk")
        .to_owned()
}

fn plural(count: usize) -> &'static str {
    if count == 1 { "" } else { "s" }
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

    fn tracked_diff(id: &str) -> TrackedAgentDiff {
        TrackedAgentDiff {
            id: id.to_owned(),
            conversation_id: "conv-a".to_owned(),
            path: Some("src/lib.rs".to_owned()),
            abs: Some("/repo/src/lib.rs".to_owned()),
            rel: Some("src/lib.rs".to_owned()),
            line: 20,
            column: 1,
            source: "appserver_diff".to_owned(),
            created_at: "unix_ms:1".to_owned(),
            repo_root: Some("/repo".to_owned()),
            diff_text:
                "diff --git a/src/lib.rs b/src/lib.rs\n@@ -10,2 +20,4 @@ fn example() {\n+added"
                    .to_owned(),
            diff_bytes: 80,
            additions: 1,
            deletions: 0,
        }
    }

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
    fn parses_hunk_header_ranges() {
        assert_eq!(
            parse_hunk_header("@@ -10,2 +20,4 @@ fn example() {"),
            Some((10, 2, 20, 4))
        );
    }

    #[test]
    fn lists_tracked_diffs_newest_first_for_bulk_reject() {
        let ledger = AgentEditLedger::default();
        {
            let mut guard = ledger.inner.lock().unwrap();
            guard.insert(
                "conv-a".to_owned(),
                vec![tracked_diff("oldest"), tracked_diff("newest")],
            );
        }

        let ids: Vec<String> = ledger
            .list_newest_first("conv-a")
            .unwrap()
            .into_iter()
            .map(|entry| entry.id)
            .collect();

        assert_eq!(ids, vec!["newest", "oldest"]);
    }

    #[test]
    fn builds_inline_publish_payload() {
        let diff = tracked_diff("diff-1");
        let payload = diff.inline_publish_payload().unwrap();
        assert_eq!(payload["conversationId"], "conv-a");
        assert_eq!(payload["uri"], "file:///repo/src/lib.rs");
        let edits = payload["edits"].as_array().unwrap();
        assert_eq!(edits[0]["editId"], "diff-1");
        assert_eq!(edits[0]["hunks"][0]["modifiedRange"]["startLineNumber"], 20);
    }

    #[test]
    fn builds_inline_clear_payload() {
        let diff = tracked_diff("diff-1");
        let payload = diff.inline_clear_payload().unwrap();
        assert_eq!(payload["uri"], "file:///repo/src/lib.rs");
        assert_eq!(payload["editId"], "diff-1");
        assert_eq!(payload["diffId"], "diff-1");
        assert_eq!(payload["conversationId"], "conv-a");
        assert_eq!(payload["projectPath"], "/repo");
        assert_eq!(payload["source"], "appserver_diff");
    }

    #[test]
    fn inline_hunks_include_deleted_line_text() {
        let hunks = inline_hunks_from_diff(
            "edit-1",
            "diff --git a/file b/file\n@@ -520,1 +520,0 @@\n-deleted text",
            520,
            0,
            1,
        );
        assert_eq!(hunks.len(), 1);
        let hunk = &hunks[0];
        assert_eq!(hunk["kind"], "deleted");
        assert_eq!(hunk["originalRange"]["startLineNumber"], 520);
        assert_eq!(hunk["originalRange"]["endLineNumber"], 520);
        assert_eq!(hunk["modifiedRange"]["startLineNumber"], 520);
        assert_eq!(hunk["originalLines"], json!(["deleted text"]));
        assert_eq!(hunk["modifiedLines"], json!([]));
    }

    #[test]
    fn inline_hunks_include_added_line_text() {
        let hunks = inline_hunks_from_diff(
            "edit-1",
            "diff --git a/file b/file\n@@ -10,0 +11,1 @@\n+added text",
            11,
            1,
            0,
        );
        assert_eq!(hunks.len(), 1);
        let hunk = &hunks[0];
        assert_eq!(hunk["kind"], "added");
        assert_eq!(hunk["originalRange"]["startLineNumber"], 10);
        assert_eq!(hunk["modifiedRange"]["startLineNumber"], 11);
        assert_eq!(hunk["originalLines"], json!([]));
        assert_eq!(hunk["modifiedLines"], json!(["added text"]));
    }

    #[test]
    fn inline_hunks_include_modified_line_text() {
        let hunks = inline_hunks_from_diff(
            "edit-1",
            "diff --git a/file b/file\n@@ -10,1 +10,1 @@\n-old text\n+new text",
            10,
            1,
            1,
        );
        assert_eq!(hunks.len(), 1);
        let hunk = &hunks[0];
        assert_eq!(hunk["kind"], "modified");
        assert_eq!(hunk["originalRange"]["startLineNumber"], 10);
        assert_eq!(hunk["modifiedRange"]["startLineNumber"], 10);
        assert_eq!(hunk["originalLines"], json!(["old text"]));
        assert_eq!(hunk["modifiedLines"], json!(["new text"]));
    }

    #[test]
    fn inline_hunks_split_changed_blocks_on_context() {
        let hunks = inline_hunks_from_diff(
            "edit-1",
            "diff --git a/file b/file\n@@ -1,4 +1,5 @@\n context\n-old text\n+new text\n context\n+added text",
            1,
            2,
            1,
        );
        assert_eq!(hunks.len(), 2);
        assert_eq!(hunks[0]["kind"], "modified");
        assert_eq!(hunks[0]["originalLines"], json!(["old text"]));
        assert_eq!(hunks[0]["modifiedLines"], json!(["new text"]));
        assert_eq!(hunks[1]["kind"], "added");
        assert_eq!(hunks[1]["originalLines"], json!([]));
        assert_eq!(hunks[1]["modifiedLines"], json!(["added text"]));
    }
}
