use als_adapter_protocol::JsonMap;
use anyhow::{Context, Result, anyhow, bail};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{
    fs,
    io::{BufRead, BufReader, Read, Seek, SeekFrom, Write},
    path::PathBuf,
    sync::{Arc, Mutex},
    time::{SystemTime, UNIX_EPOCH},
};

#[derive(Clone, Debug)]
pub struct ConversationStore {
    root: PathBuf,
    lock: Arc<Mutex<()>>,
}

impl ConversationStore {
    pub fn new(data_dir: PathBuf) -> Self {
        Self {
            root: data_dir.join("conversations"),
            lock: Arc::new(Mutex::new(())),
        }
    }

    pub fn create(&self, request: CreateConversationRequest) -> Result<ConversationMeta> {
        let _guard = self
            .lock
            .lock()
            .map_err(|_| anyhow!("conversation store lock poisoned"))?;
        let conversation_id = request
            .conversation_id
            .map(|value| sanitize_conversation_id(&value))
            .unwrap_or_else(new_conversation_id);
        let mut meta = self.default_meta(&conversation_id);
        meta.title = request.title;
        meta.agent_type = request.agent_type;
        meta.settings = request.settings;
        self.write_meta_unlocked(&meta)?;
        Ok(meta)
    }

    pub fn list(&self) -> Result<Vec<ConversationSummary>> {
        let _guard = self
            .lock
            .lock()
            .map_err(|_| anyhow!("conversation store lock poisoned"))?;
        if !self.root.exists() {
            return Ok(Vec::new());
        }

        let mut summaries = Vec::new();
        for entry in fs::read_dir(&self.root).context("failed to read conversations directory")? {
            let entry = entry?;
            if !entry.file_type()?.is_dir() {
                continue;
            }
            let meta_path = entry.path().join("meta.json");
            if !meta_path.exists() {
                continue;
            }
            let meta = read_meta(&meta_path)?;
            summaries.push(ConversationSummary::from(meta));
        }
        summaries.sort_by(|left, right| right.updated_at.cmp(&left.updated_at));
        Ok(summaries)
    }

    pub fn load_meta(&self, conversation_id: &str) -> Result<ConversationMeta> {
        let _guard = self
            .lock
            .lock()
            .map_err(|_| anyhow!("conversation store lock poisoned"))?;
        let safe_id = sanitize_conversation_id(conversation_id);
        let path = self.conversation_dir_unlocked(&safe_id).join("meta.json");
        if path.exists() {
            return read_meta(&path);
        }
        let meta = self.default_meta(&safe_id);
        self.write_meta_unlocked(&meta)?;
        Ok(meta)
    }

    pub fn update_meta(
        &self,
        conversation_id: &str,
        update: ConversationMetaUpdate,
    ) -> Result<ConversationMeta> {
        let _guard = self
            .lock
            .lock()
            .map_err(|_| anyhow!("conversation store lock poisoned"))?;
        let mut meta = self.load_or_default_meta_unlocked(conversation_id)?;
        if let Some(settings) = update.settings {
            meta.settings = settings;
        }
        if let Some(thread_id) = update.thread_id {
            meta.thread_id = Some(thread_id);
        }
        if let Some(title) = update.title {
            meta.title = Some(title);
        }
        if let Some(draft) = update.draft {
            meta.draft = Some(draft);
        }
        meta.updated_at = utc_ts();
        self.write_meta_unlocked(&meta)?;
        Ok(meta)
    }

    pub fn delete(&self, conversation_id: &str) -> Result<bool> {
        let _guard = self
            .lock
            .lock()
            .map_err(|_| anyhow!("conversation store lock poisoned"))?;
        let safe_id = sanitize_conversation_id(conversation_id);
        let dir = self.conversation_dir_unlocked(&safe_id);
        if !dir.exists() {
            return Ok(false);
        }
        fs::remove_dir_all(&dir)
            .with_context(|| format!("failed to delete conversation {}", dir.display()))?;
        Ok(true)
    }

    pub fn append_transcript(&self, conversation_id: &str, mut entry: Value) -> Result<Value> {
        let _guard = self
            .lock
            .lock()
            .map_err(|_| anyhow!("conversation store lock poisoned"))?;
        let mut meta = self.load_or_default_meta_unlocked(conversation_id)?;
        let order_id = meta.next_transcript_order_id;
        meta.next_transcript_order_id += 1;
        meta.transcript_line_count = Some(
            meta.transcript_line_count.unwrap_or_else(|| {
                count_transcript_lines(
                    &self
                        .conversation_dir_unlocked(&meta.conversation_id)
                        .join("transcript.jsonl"),
                )
                .unwrap_or_default()
            }) + 1,
        );
        meta.updated_at = utc_ts();

        let object = entry
            .as_object_mut()
            .ok_or_else(|| anyhow!("transcript entry must be a JSON object"))?;
        object
            .entry("conversation_id")
            .or_insert_with(|| Value::String(meta.conversation_id.clone()));
        object
            .entry("order_id")
            .or_insert_with(|| Value::Number(order_id.into()));
        object
            .entry("ts")
            .or_insert_with(|| Value::String(utc_ts()));

        let dir = self.conversation_dir_unlocked(&meta.conversation_id);
        fs::create_dir_all(&dir)
            .with_context(|| format!("failed to create conversation dir {}", dir.display()))?;
        let mut file = fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(dir.join("transcript.jsonl"))
            .context("failed to open transcript for append")?;
        writeln!(file, "{}", serde_json::to_string(&entry)?)
            .context("failed to append transcript row")?;
        self.write_meta_unlocked(&meta)?;
        Ok(entry)
    }

    pub fn read_transcript(&self, conversation_id: &str) -> Result<Vec<Value>> {
        let _guard = self
            .lock
            .lock()
            .map_err(|_| anyhow!("conversation store lock poisoned"))?;
        let safe_id = sanitize_conversation_id(conversation_id);
        let path = self
            .conversation_dir_unlocked(&safe_id)
            .join("transcript.jsonl");
        if !path.exists() {
            return Ok(Vec::new());
        }
        let content = fs::read_to_string(&path)
            .with_context(|| format!("failed to read transcript {}", path.display()))?;
        content
            .lines()
            .filter(|line| !line.trim().is_empty())
            .map(|line| serde_json::from_str(line).context("invalid transcript JSONL row"))
            .collect()
    }

    pub fn read_transcript_chunk(
        &self,
        conversation_id: &str,
        offset: TranscriptOffset,
        limit: usize,
    ) -> Result<TranscriptChunk> {
        let _guard = self
            .lock
            .lock()
            .map_err(|_| anyhow!("conversation store lock poisoned"))?;
        let safe_id = sanitize_conversation_id(conversation_id);
        let path = self
            .conversation_dir_unlocked(&safe_id)
            .join("transcript.jsonl");
        if !path.exists() {
            return Ok(TranscriptChunk {
                offset: 0,
                total_count: 0,
                rows: Vec::new(),
            });
        }
        let limit = limit.max(1);
        let meta_path = self.conversation_dir_unlocked(&safe_id).join("meta.json");
        let known_total_count = if meta_path.exists() {
            read_meta(&meta_path)?.transcript_line_count
        } else {
            None
        };
        match offset {
            TranscriptOffset::Absolute(offset) => {
                read_transcript_chunk_at(&path, offset, limit, known_total_count)
            }
            TranscriptOffset::Latest => {
                read_latest_transcript_chunk(&path, limit, known_total_count)
            }
        }
    }

    fn load_or_default_meta_unlocked(&self, conversation_id: &str) -> Result<ConversationMeta> {
        let safe_id = sanitize_conversation_id(conversation_id);
        let path = self.conversation_dir_unlocked(&safe_id).join("meta.json");
        if path.exists() {
            read_meta(&path)
        } else {
            Ok(self.default_meta(&safe_id))
        }
    }

    fn write_meta_unlocked(&self, meta: &ConversationMeta) -> Result<()> {
        validate_safe_conversation_id(&meta.conversation_id)?;
        let dir = self.conversation_dir_unlocked(&meta.conversation_id);
        fs::create_dir_all(&dir)
            .with_context(|| format!("failed to create conversation dir {}", dir.display()))?;
        fs::write(
            dir.join("meta.json"),
            serde_json::to_string_pretty(meta).context("failed to serialize conversation meta")?,
        )
        .context("failed to write conversation meta")?;
        Ok(())
    }

    fn conversation_dir_unlocked(&self, conversation_id: &str) -> PathBuf {
        self.root.join(conversation_id)
    }

    fn default_meta(&self, conversation_id: &str) -> ConversationMeta {
        let now = utc_ts();
        ConversationMeta {
            conversation_id: conversation_id.to_owned(),
            title: None,
            agent_type: None,
            thread_id: None,
            status: "draft".to_owned(),
            created_at: now.clone(),
            updated_at: now,
            settings: JsonMap::new(),
            draft: None,
            next_transcript_order_id: 0,
            transcript_line_count: Some(0),
        }
    }
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub struct CreateConversationRequest {
    pub conversation_id: Option<String>,
    pub title: Option<String>,
    pub agent_type: Option<String>,
    #[serde(default)]
    pub settings: JsonMap,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct ConversationMeta {
    pub conversation_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub title: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub agent_type: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub thread_id: Option<String>,
    pub status: String,
    pub created_at: String,
    pub updated_at: String,
    #[serde(default)]
    pub settings: JsonMap,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub draft: Option<String>,
    #[serde(default)]
    pub next_transcript_order_id: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub transcript_line_count: Option<usize>,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub struct ConversationMetaUpdate {
    #[serde(default)]
    pub settings: Option<JsonMap>,
    #[serde(default)]
    pub thread_id: Option<String>,
    #[serde(default)]
    pub title: Option<String>,
    #[serde(default)]
    pub draft: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct ConversationSummary {
    pub conversation_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub title: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub agent_type: Option<String>,
    pub status: String,
    pub created_at: String,
    pub updated_at: String,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TranscriptOffset {
    Absolute(usize),
    Latest,
}

#[derive(Clone, Debug, PartialEq)]
pub struct TranscriptChunk {
    pub offset: usize,
    pub total_count: usize,
    pub rows: Vec<String>,
}

impl From<ConversationMeta> for ConversationSummary {
    fn from(meta: ConversationMeta) -> Self {
        Self {
            conversation_id: meta.conversation_id,
            title: meta.title,
            agent_type: meta.agent_type,
            status: meta.status,
            created_at: meta.created_at,
            updated_at: meta.updated_at,
        }
    }
}

fn read_meta(path: &PathBuf) -> Result<ConversationMeta> {
    let raw = fs::read_to_string(path)
        .with_context(|| format!("failed to read conversation meta {}", path.display()))?;
    serde_json::from_str(&raw)
        .with_context(|| format!("invalid conversation meta {}", path.display()))
}

fn read_transcript_chunk_at(
    path: &PathBuf,
    offset: usize,
    limit: usize,
    known_total_count: Option<usize>,
) -> Result<TranscriptChunk> {
    let file = fs::File::open(path)
        .with_context(|| format!("failed to open transcript {}", path.display()))?;
    let reader = BufReader::new(file);
    let mut rows = Vec::new();
    let mut total_count = 0usize;
    for line in reader.lines() {
        let line = line.with_context(|| format!("failed to read transcript {}", path.display()))?;
        if line.trim().is_empty() {
            continue;
        }
        if total_count >= offset && rows.len() < limit {
            rows.push(line);
        }
        total_count += 1;
        if known_total_count.is_some() && rows.len() >= limit {
            break;
        }
    }
    let total_count = known_total_count.unwrap_or(total_count);
    Ok(TranscriptChunk {
        offset: offset.min(total_count),
        total_count,
        rows,
    })
}

fn read_latest_transcript_chunk(
    path: &PathBuf,
    limit: usize,
    known_total_count: Option<usize>,
) -> Result<TranscriptChunk> {
    let total_count = match known_total_count {
        Some(count) => count,
        None => count_transcript_lines(path)?,
    };
    let rows = read_tail_lines(path, limit)?;
    Ok(TranscriptChunk {
        offset: total_count.saturating_sub(rows.len()),
        total_count,
        rows,
    })
}

fn count_transcript_lines(path: &PathBuf) -> Result<usize> {
    if !path.exists() {
        return Ok(0);
    }
    let file = fs::File::open(path)
        .with_context(|| format!("failed to open transcript {}", path.display()))?;
    let reader = BufReader::new(file);
    let mut total_count = 0usize;
    for line in reader.lines() {
        let line = line.with_context(|| format!("failed to read transcript {}", path.display()))?;
        if line.trim().is_empty() {
            continue;
        }
        total_count += 1;
    }
    Ok(total_count)
}

fn read_tail_lines(path: &PathBuf, limit: usize) -> Result<Vec<String>> {
    if limit == 0 {
        return Ok(Vec::new());
    }
    let mut file = fs::File::open(path)
        .with_context(|| format!("failed to open transcript {}", path.display()))?;
    let file_len = file.metadata()?.len();
    if file_len == 0 {
        return Ok(Vec::new());
    }

    let mut window_size = 64 * 1024u64;
    loop {
        let read_size = window_size.min(file_len);
        file.seek(SeekFrom::Start(file_len - read_size))
            .with_context(|| format!("failed to seek transcript {}", path.display()))?;
        let mut buf = vec![0u8; read_size as usize];
        file.read_exact(&mut buf)
            .with_context(|| format!("failed to read transcript tail {}", path.display()))?;
        let at_start = read_size == file_len;
        let mut parts = buf.split(|byte| *byte == b'\n').collect::<Vec<_>>();
        if !at_start && !parts.is_empty() {
            parts.remove(0);
        }
        let mut lines = parts
            .into_iter()
            .filter(|line| !line.iter().all(|byte| byte.is_ascii_whitespace()))
            .map(|line| String::from_utf8_lossy(line).to_string())
            .collect::<Vec<_>>();
        if lines.len() >= limit || at_start {
            let keep_from = lines.len().saturating_sub(limit);
            return Ok(lines.split_off(keep_from));
        }
        window_size = (window_size * 2).min(file_len);
    }
}

fn sanitize_conversation_id(value: &str) -> String {
    let safe = value
        .chars()
        .map(|ch| {
            if ch.is_ascii_alphanumeric() || matches!(ch, '.' | '_' | '-') {
                ch
            } else {
                '_'
            }
        })
        .collect::<String>()
        .trim_matches('_')
        .to_owned();
    if safe.is_empty() {
        "unknown".to_owned()
    } else {
        safe
    }
}

fn validate_safe_conversation_id(value: &str) -> Result<()> {
    if sanitize_conversation_id(value) == value {
        Ok(())
    } else {
        bail!("invalid conversation id")
    }
}

fn new_conversation_id() -> String {
    format!("conv_{}_{}", unix_millis(), std::process::id())
}

fn utc_ts() -> String {
    format!("unix_ms:{}", unix_millis())
}

fn unix_millis() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn creates_meta_and_appends_ordered_transcript_rows() {
        let root = std::env::temp_dir().join(format!("als-rs-store-test-{}", unix_millis()));
        let store = ConversationStore::new(root.clone());
        let meta = store
            .create(CreateConversationRequest {
                conversation_id: Some("test/conversation".to_owned()),
                title: Some("Test".to_owned()),
                agent_type: Some("copilot-sdk".to_owned()),
                settings: JsonMap::new(),
            })
            .unwrap();

        assert_eq!(meta.conversation_id, "test_conversation");
        assert_eq!(meta.title.as_deref(), Some("Test"));

        let first = store
            .append_transcript("test/conversation", json!({"role": "user", "text": "hi"}))
            .unwrap();
        let second = store
            .append_transcript(
                "test/conversation",
                json!({"role": "assistant", "text": "hello"}),
            )
            .unwrap();

        assert_eq!(first["order_id"], 0);
        assert_eq!(second["order_id"], 1);
        let rows = store.read_transcript("test/conversation").unwrap();
        assert_eq!(rows.len(), 2);
        assert_eq!(store.list().unwrap().len(), 1);

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn reads_latest_transcript_chunk_without_full_materialization() {
        let root = std::env::temp_dir().join(format!("als-rs-store-latest-test-{}", unix_millis()));
        let store = ConversationStore::new(root.clone());
        store
            .create(CreateConversationRequest {
                conversation_id: Some("latest-test".to_owned()),
                ..CreateConversationRequest::default()
            })
            .unwrap();
        for idx in 0..5 {
            store
                .append_transcript(
                    "latest-test",
                    json!({"role": "user", "text": format!("row {idx}")}),
                )
                .unwrap();
        }

        let latest = store
            .read_transcript_chunk("latest-test", TranscriptOffset::Latest, 2)
            .unwrap();
        assert_eq!(latest.total_count, 5);
        assert_eq!(latest.offset, 3);
        assert_eq!(latest.rows.len(), 2);
        assert!(latest.rows[0].contains("\"text\":\"row 3\""));
        assert!(latest.rows[1].contains("\"text\":\"row 4\""));
        assert_eq!(
            store
                .load_meta("latest-test")
                .unwrap()
                .transcript_line_count,
            Some(5)
        );

        let middle = store
            .read_transcript_chunk("latest-test", TranscriptOffset::Absolute(1), 2)
            .unwrap();
        assert_eq!(middle.total_count, 5);
        assert_eq!(middle.offset, 1);
        assert_eq!(middle.rows.len(), 2);
        assert!(middle.rows[0].contains("\"text\":\"row 1\""));
        assert!(middle.rows[1].contains("\"text\":\"row 2\""));

        let _ = fs::remove_dir_all(root);
    }
}
