use crate::composer_sync::ComposerSelection;
use crate::transcript_card_projection::{
    IndexedProjectionEvent, TranscriptCardIndex, TranscriptCardRecipe, apply_projection_metadata,
};
use als_adapter_protocol::JsonMap;
use anyhow::{Context, Result, anyhow, bail};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use std::{
    collections::HashMap,
    fs,
    io::{BufRead, BufReader, Seek, SeekFrom, Write},
    path::PathBuf,
    sync::{Arc, Mutex},
    time::{SystemTime, UNIX_EPOCH},
};

#[derive(Clone, Debug)]
pub struct ConversationStore {
    root: PathBuf,
    lock: Arc<Mutex<()>>,
    transcript_indexes: Arc<Mutex<HashMap<String, TranscriptLineIndex>>>,
    transcript_projections: Arc<Mutex<HashMap<(String, String), TranscriptProjectionCursor>>>,
}

#[derive(Clone, Debug)]
struct TranscriptLineIndex {
    file_len: u64,
    line_offsets: Vec<u64>,
    cards: TranscriptCardIndex,
}

#[derive(Clone, Debug)]
struct TranscriptProjectionCursor {
    start_card: usize,
    window_cards: usize,
    shift_cards: usize,
    at_tail: bool,
    revision: u64,
}

impl ConversationStore {
    pub fn new(data_dir: PathBuf) -> Self {
        Self {
            root: data_dir.join("conversations"),
            lock: Arc::new(Mutex::new(())),
            transcript_indexes: Arc::new(Mutex::new(HashMap::new())),
            transcript_projections: Arc::new(Mutex::new(HashMap::new())),
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
        meta.settings = request.settings;
        if let Some(value) = request.extension_id.or(request.agent_type) {
            set_conversation_extension(&mut meta, value);
        }
        if let Some(value) = request.thread_id {
            meta.thread_id = Some(value.clone());
            meta.provider_session_id = Some(value);
            meta.status = "active".to_owned();
        }
        if let Some(value) = request.cwd {
            meta.cwd = Some(value.clone());
            set_string_setting(&mut meta.settings, "cwd", value);
        }
        if let Some(value) = request.label {
            meta.label = Some(value.clone());
            set_string_setting(&mut meta.settings, "label", value);
        }
        if let Some(value) = request.alias {
            meta.alias = Some(value.clone());
            set_string_setting(&mut meta.settings, "alias", value);
        }
        if request.pinned {
            meta.pinned = true;
        }
        sync_meta_from_settings(&mut meta);
        self.write_meta_unlocked(&meta)?;
        Ok(meta)
    }

    pub fn allocate_conversation_id(&self) -> String {
        new_conversation_id()
    }

    pub fn fork_from(&self, request: ForkConversationRequest) -> Result<ConversationMeta> {
        let _guard = self
            .lock
            .lock()
            .map_err(|_| anyhow!("conversation store lock poisoned"))?;
        let source_id = sanitize_conversation_id(&request.source_conversation_id);
        let target_id = request
            .conversation_id
            .map(|value| sanitize_conversation_id(&value))
            .unwrap_or_else(new_conversation_id);
        validate_safe_conversation_id(&source_id)?;
        validate_safe_conversation_id(&target_id)?;
        if source_id == target_id {
            bail!("fork target conversation id must differ from source");
        }

        let source_dir = self.conversation_dir_unlocked(&source_id);
        let source_meta_path = source_dir.join("meta.json");
        if !source_meta_path.exists() {
            bail!("source conversation does not exist: {source_id}");
        }
        let target_dir = self.conversation_dir_unlocked(&target_id);
        if target_dir.exists() {
            bail!("target conversation already exists: {target_id}");
        }

        let source = read_meta(&source_meta_path)?;
        let now = utc_ts();
        let provider_session_id = nonempty_owned(request.provider_session_id)
            .or_else(|| source.provider_session_id.clone())
            .or_else(|| source.thread_id.clone())
            .ok_or_else(|| anyhow!("fork provider_session_id is required"))?;
        let source_provider_session_id = source
            .provider_session_id
            .clone()
            .or_else(|| source.thread_id.clone());

        fs::create_dir_all(&target_dir).with_context(|| {
            format!("failed to create conversation dir {}", target_dir.display())
        })?;
        let (line_count, next_order_id) = copy_transcript_for_fork(
            &source_dir.join("transcript.jsonl"),
            &target_dir,
            &target_id,
        )?;

        let mut meta = source;
        meta.conversation_id = target_id.clone();
        meta.title = request.title.or_else(|| {
            meta.title
                .as_deref()
                .map(|title| format!("Fork of {title}"))
        });
        meta.thread_id = Some(provider_session_id.clone());
        meta.provider_session_id = Some(provider_session_id);
        meta.forked_from_conversation_id = Some(source_id);
        meta.forked_from_provider_session_id = source_provider_session_id;
        meta.pinned = false;
        meta.pinned_order = None;
        meta.pending_approvals = JsonMap::new();
        meta.pending_approvals_revision = 0;
        meta.status = "active".to_owned();
        meta.created_at = now.clone();
        meta.updated_at = now;
        if let Some(settings) = request.settings {
            meta.settings = settings;
            sync_meta_from_settings(&mut meta);
        }
        meta.draft = None;
        meta.draft_revision = 0;
        meta.draft_selection = None;
        meta.ask_user_msg_counter = 0;
        meta.next_transcript_order_id = next_order_id;
        meta.transcript_line_count = Some(line_count);
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
            match read_meta(&meta_path) {
                Ok(meta) => summaries.push(ConversationSummary::from(meta)),
                Err(error) => {
                    let conversation_id = entry.file_name().to_string_lossy().into_owned();
                    summaries.push(ConversationSummary::corrupt(
                        conversation_id,
                        format!("{error:#}"),
                        file_modified_ts(&meta_path),
                    ));
                }
            }
        }
        summaries.sort_by(compare_summaries_for_display);
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

    pub fn load_meta_if_exists(&self, conversation_id: &str) -> Result<Option<ConversationMeta>> {
        let _guard = self
            .lock
            .lock()
            .map_err(|_| anyhow!("conversation store lock poisoned"))?;
        let safe_id = sanitize_conversation_id(conversation_id);
        let path = self.conversation_dir_unlocked(&safe_id).join("meta.json");
        if path.exists() {
            return read_meta(&path).map(Some);
        }
        Ok(None)
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
            merge_settings_patch(&mut meta.settings, settings);
        }
        if let Some(thread_id) = update.thread_id {
            meta.thread_id = Some(thread_id.clone());
            meta.provider_session_id = Some(thread_id);
            meta.status = "active".to_owned();
        }
        if let Some(title) = update.title {
            meta.title = Some(title);
        }
        if let Some(draft) = update.draft {
            meta.draft = Some(draft);
        }
        if let Some(extension_id) = update.extension_id.or(update.agent_type) {
            set_conversation_extension(&mut meta, extension_id);
        }
        if let Some(cwd) = update.cwd {
            meta.cwd = Some(cwd.clone());
            set_string_setting(&mut meta.settings, "cwd", cwd);
        }
        if let Some(label) = update.label {
            meta.label = Some(label.clone());
            set_string_setting(&mut meta.settings, "label", label);
        }
        if let Some(alias) = update.alias {
            meta.alias = Some(alias.clone());
            set_string_setting(&mut meta.settings, "alias", alias);
        }
        if let Some(pinned) = update.pinned {
            meta.pinned = pinned;
            if !pinned {
                meta.pinned_order = None;
            }
        }
        sync_meta_from_settings(&mut meta);
        meta.updated_at = utc_ts();
        self.write_meta_unlocked(&meta)?;
        Ok(meta)
    }

    pub fn set_draft(
        &self,
        conversation_id: &str,
        draft: String,
        selection: Option<ComposerSelection>,
    ) -> Result<ConversationMeta> {
        let _guard = self
            .lock
            .lock()
            .map_err(|_| anyhow!("conversation store lock poisoned"))?;
        let mut meta = self.load_or_default_meta_unlocked(conversation_id)?;
        meta.draft = Some(draft);
        meta.draft_revision = meta.draft_revision.saturating_add(1);
        if let Some(selection) = selection {
            meta.draft_selection = Some(selection);
        }
        meta.updated_at = utc_ts();
        self.write_meta_unlocked(&meta)?;
        Ok(meta)
    }

    pub fn upsert_pending_approval(
        &self,
        conversation_id: &str,
        request_id: &str,
        mut descriptor: JsonMap,
    ) -> Result<ConversationMeta> {
        let _guard = self
            .lock
            .lock()
            .map_err(|_| anyhow!("conversation store lock poisoned"))?;
        let mut meta = self.load_or_default_meta_unlocked(conversation_id)?;
        let request_id = request_id.trim();
        if request_id.is_empty() {
            bail!("request_id is required");
        }
        descriptor.insert(
            "request_id".to_owned(),
            Value::String(request_id.to_owned()),
        );
        descriptor
            .entry("conversation_id".to_owned())
            .or_insert_with(|| Value::String(meta.conversation_id.clone()));
        descriptor
            .entry("status".to_owned())
            .or_insert_with(|| Value::String("pending".to_owned()));
        descriptor
            .entry("created_at".to_owned())
            .or_insert_with(|| Value::String(utc_ts()));
        descriptor.insert("updated_at".to_owned(), Value::String(utc_ts()));
        meta.pending_approvals_revision = meta.pending_approvals_revision.saturating_add(1);
        stamp_pending_approval_revision(&mut descriptor, meta.pending_approvals_revision);
        meta.pending_approvals
            .insert(request_id.to_owned(), Value::Object(descriptor));
        meta.updated_at = utc_ts();
        self.write_meta_unlocked(&meta)?;
        Ok(meta)
    }

    pub fn replace_pending_approval_for_requestor(
        &self,
        conversation_id: &str,
        request_id: &str,
        requestor_id: &str,
        request_method: &str,
        mut descriptor: JsonMap,
    ) -> Result<(ConversationMeta, Vec<JsonMap>)> {
        let _guard = self
            .lock
            .lock()
            .map_err(|_| anyhow!("conversation store lock poisoned"))?;
        let mut meta = self.load_or_default_meta_unlocked(conversation_id)?;
        let request_id = request_id.trim();
        let requestor_id = requestor_id.trim();
        let request_method = request_method.trim();
        if request_id.is_empty() {
            bail!("request_id is required");
        }
        if requestor_id.is_empty() {
            bail!("requestor_id is required");
        }
        if request_method.is_empty() {
            bail!("request_method is required");
        }

        let stale_ids = meta
            .pending_approvals
            .iter()
            .filter_map(|(pending_id, value)| {
                let pending = value.as_object()?;
                let pending_method = pending
                    .get("request_method")
                    .and_then(Value::as_str)
                    .map(str::trim)
                    .unwrap_or_default();
                if !pending_method.eq_ignore_ascii_case(request_method) {
                    return None;
                }
                let pending_requestor = pending
                    .get("requestor_id")
                    .or_else(|| pending.get("requestorId"))
                    .and_then(Value::as_str)
                    .map(str::trim)
                    .filter(|value| !value.is_empty())
                    .unwrap_or(meta.conversation_id.as_str());
                (pending_requestor == requestor_id && pending_id != request_id)
                    .then(|| pending_id.clone())
            })
            .collect::<Vec<_>>();
        let mut replaced = Vec::with_capacity(stale_ids.len());
        for stale_id in stale_ids {
            if let Some(value) = meta.pending_approvals.remove(&stale_id)
                && let Some(pending) = value.as_object().cloned()
            {
                replaced.push(pending);
            }
        }

        descriptor.insert(
            "request_id".to_owned(),
            Value::String(request_id.to_owned()),
        );
        descriptor.insert(
            "requestor_id".to_owned(),
            Value::String(requestor_id.to_owned()),
        );
        descriptor
            .entry("conversation_id".to_owned())
            .or_insert_with(|| Value::String(meta.conversation_id.clone()));
        descriptor
            .entry("status".to_owned())
            .or_insert_with(|| Value::String("pending".to_owned()));
        descriptor
            .entry("created_at".to_owned())
            .or_insert_with(|| Value::String(utc_ts()));
        descriptor.insert("updated_at".to_owned(), Value::String(utc_ts()));
        meta.pending_approvals_revision = meta.pending_approvals_revision.saturating_add(1);
        stamp_pending_approval_revision(&mut descriptor, meta.pending_approvals_revision);
        meta.pending_approvals
            .insert(request_id.to_owned(), Value::Object(descriptor));
        meta.updated_at = utc_ts();
        self.write_meta_unlocked(&meta)?;
        Ok((meta, replaced))
    }

    pub fn remove_pending_approval(
        &self,
        conversation_id: &str,
        request_id: &str,
    ) -> Result<Option<JsonMap>> {
        let _guard = self
            .lock
            .lock()
            .map_err(|_| anyhow!("conversation store lock poisoned"))?;
        let mut meta = self.load_or_default_meta_unlocked(conversation_id)?;
        let removed = meta
            .pending_approvals
            .remove(request_id.trim())
            .and_then(|value| value.as_object().cloned());
        if removed.is_some() {
            meta.pending_approvals_revision = meta.pending_approvals_revision.saturating_add(1);
            meta.updated_at = utc_ts();
            self.write_meta_unlocked(&meta)?;
        }
        Ok(removed)
    }

    pub fn find_pending_approval(&self, request_id: &str) -> Result<Option<(String, JsonMap)>> {
        let _guard = self
            .lock
            .lock()
            .map_err(|_| anyhow!("conversation store lock poisoned"))?;
        let request_id = request_id.trim();
        if request_id.is_empty() || !self.root.exists() {
            return Ok(None);
        }
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
            let Some(descriptor) = meta
                .pending_approvals
                .get(request_id)
                .and_then(Value::as_object)
                .cloned()
            else {
                continue;
            };
            return Ok(Some((meta.conversation_id, descriptor)));
        }
        Ok(None)
    }

    pub fn next_ask_user_msg_id(&self, conversation_id: &str) -> Result<u64> {
        let _guard = self
            .lock
            .lock()
            .map_err(|_| anyhow!("conversation store lock poisoned"))?;
        let mut meta = self.load_or_default_meta_unlocked(conversation_id)?;
        let current = meta.ask_user_msg_counter;
        meta.ask_user_msg_counter += 1;
        meta.updated_at = utc_ts();
        self.write_meta_unlocked(&meta)?;
        Ok(current)
    }

    pub fn set_pinned_conversations(&self, requested: Vec<String>) -> Result<Vec<String>> {
        let _guard = self
            .lock
            .lock()
            .map_err(|_| anyhow!("conversation store lock poisoned"))?;
        if !self.root.exists() {
            return Ok(Vec::new());
        }

        let mut metas = Vec::new();
        for entry in fs::read_dir(&self.root).context("failed to read conversations directory")? {
            let entry = entry?;
            if !entry.file_type()?.is_dir() {
                continue;
            }
            let meta_path = entry.path().join("meta.json");
            if meta_path.exists() {
                metas.push(read_meta(&meta_path)?);
            }
        }
        let valid_ids = metas
            .iter()
            .map(|meta| meta.conversation_id.clone())
            .collect::<std::collections::HashSet<_>>();
        let mut pinned = Vec::new();
        for item in requested {
            let safe_id = sanitize_conversation_id(&item);
            if valid_ids.contains(&safe_id) && !pinned.contains(&safe_id) {
                pinned.push(safe_id);
            }
        }

        let now = utc_ts();
        for mut meta in metas {
            if let Some(index) = pinned.iter().position(|id| id == &meta.conversation_id) {
                meta.pinned = true;
                meta.pinned_order = Some(index as u64);
            } else {
                meta.pinned = false;
                meta.pinned_order = None;
            }
            meta.updated_at = now.clone();
            self.write_meta_unlocked(&meta)?;
        }
        Ok(pinned)
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
        self.transcript_indexes
            .lock()
            .map_err(|_| anyhow!("transcript index lock poisoned"))?
            .remove(&safe_id);
        self.transcript_projections
            .lock()
            .map_err(|_| anyhow!("transcript projection lock poisoned"))?
            .retain(|(_, conversation_id), _| conversation_id != &safe_id);
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
        update_meta_from_transcript_entry(&mut meta, &entry);

        let dir = self.conversation_dir_unlocked(&meta.conversation_id);
        fs::create_dir_all(&dir)
            .with_context(|| format!("failed to create conversation dir {}", dir.display()))?;
        let transcript_path = dir.join("transcript.jsonl");
        let previous_file_len = fs::metadata(&transcript_path)
            .map(|metadata| metadata.len())
            .unwrap_or(0);
        let serialized = serde_json::to_string(&entry)?;
        let mut file = fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&transcript_path)
            .context("failed to open transcript for append")?;
        writeln!(file, "{serialized}").context("failed to append transcript row")?;
        self.record_appended_transcript_rows_unlocked(
            &meta.conversation_id,
            previous_file_len,
            &[serialized.len()],
            std::slice::from_ref(&entry),
        )?;
        self.transcript_total_count_unlocked(&meta.conversation_id, &transcript_path)?;
        self.advance_tail_projections_unlocked(&meta.conversation_id)?;
        self.write_meta_unlocked(&meta)?;
        let emitted = self
            .transcript_indexes
            .lock()
            .map_err(|_| anyhow!("transcript index lock poisoned"))?
            .get(&meta.conversation_id)
            .and_then(|index| {
                index
                    .line_offsets
                    .len()
                    .checked_sub(1)
                    .and_then(|line_index| index.cards.event_metadata(line_index))
            })
            .map(|metadata| {
                apply_projection_metadata(
                    entry.clone(),
                    &metadata.card_id,
                    metadata.card_index,
                    metadata.operation,
                    metadata.parent_card_id.as_deref(),
                )
            })
            .unwrap_or(entry);
        Ok(emitted)
    }

    pub fn append_transcript_batch(
        &self,
        conversation_id: &str,
        entries: Vec<Value>,
    ) -> Result<Vec<Value>> {
        if entries.is_empty() {
            return Ok(Vec::new());
        }

        let _guard = self
            .lock
            .lock()
            .map_err(|_| anyhow!("conversation store lock poisoned"))?;
        let mut meta = self.load_or_default_meta_unlocked(conversation_id)?;
        let mut line_count = meta.transcript_line_count.unwrap_or_else(|| {
            count_transcript_lines(
                &self
                    .conversation_dir_unlocked(&meta.conversation_id)
                    .join("transcript.jsonl"),
            )
            .unwrap_or_default()
        });
        let mut rows = Vec::with_capacity(entries.len());

        for mut entry in entries {
            let order_id = meta.next_transcript_order_id;
            meta.next_transcript_order_id += 1;
            line_count += 1;

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
            update_meta_from_transcript_entry(&mut meta, &entry);
            rows.push(entry);
        }

        meta.transcript_line_count = Some(line_count);
        meta.updated_at = utc_ts();
        let dir = self.conversation_dir_unlocked(&meta.conversation_id);
        fs::create_dir_all(&dir)
            .with_context(|| format!("failed to create conversation dir {}", dir.display()))?;
        let transcript_path = dir.join("transcript.jsonl");
        let previous_file_len = fs::metadata(&transcript_path)
            .map(|metadata| metadata.len())
            .unwrap_or(0);
        let serialized_rows = rows
            .iter()
            .map(serde_json::to_string)
            .collect::<Result<Vec<_>, _>>()?;
        let mut file = fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&transcript_path)
            .context("failed to open transcript for append")?;
        for row in &serialized_rows {
            writeln!(file, "{row}").context("failed to append transcript row")?;
        }
        let row_lengths = serialized_rows.iter().map(String::len).collect::<Vec<_>>();
        self.record_appended_transcript_rows_unlocked(
            &meta.conversation_id,
            previous_file_len,
            &row_lengths,
            &rows,
        )?;
        self.advance_tail_projections_unlocked(&meta.conversation_id)?;
        self.write_meta_unlocked(&meta)?;
        Ok(rows)
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

    pub fn read_transcript_range(
        &self,
        conversation_id: &str,
        offset: TranscriptOffset,
        limit: usize,
        max_bytes: usize,
    ) -> Result<TranscriptChunk> {
        let _guard = self
            .lock
            .lock()
            .map_err(|_| anyhow!("conversation store lock poisoned"))?;
        self.read_transcript_range_unlocked(conversation_id, offset, limit, max_bytes)
    }

    pub fn project_transcript(
        &self,
        client_id: &str,
        conversation_id: &str,
        action: TranscriptProjectionAction,
        window_cards: usize,
        shift_cards: usize,
        max_bytes: usize,
    ) -> Result<TranscriptProjection> {
        let _guard = self
            .lock
            .lock()
            .map_err(|_| anyhow!("conversation store lock poisoned"))?;
        let safe_id = sanitize_conversation_id(conversation_id);
        let path = self
            .conversation_dir_unlocked(&safe_id)
            .join("transcript.jsonl");
        let window_cards = window_cards.max(1);
        let shift_cards = shift_cards.max(1).min(window_cards);
        let raw_total_count = if path.exists() {
            self.transcript_total_count_unlocked(&safe_id, &path)?
        } else {
            0
        };
        let total_cards = if path.exists() {
            self.transcript_indexes
                .lock()
                .map_err(|_| anyhow!("transcript index lock poisoned"))?
                .get(&safe_id)
                .map(|index| index.cards.total_cards())
                .unwrap_or_default()
        } else {
            0
        };
        let max_start = total_cards.saturating_sub(window_cards);
        let key = (client_id.to_owned(), safe_id.clone());
        let (start, revision) = {
            let mut projections = self
                .transcript_projections
                .lock()
                .map_err(|_| anyhow!("transcript projection lock poisoned"))?;
            let cursor = projections
                .entry(key)
                .or_insert_with(|| TranscriptProjectionCursor {
                    start_card: max_start,
                    window_cards,
                    shift_cards,
                    at_tail: true,
                    revision: 0,
                });
            cursor.window_cards = window_cards;
            cursor.shift_cards = shift_cards;
            let current_start = if cursor.at_tail {
                max_start
            } else {
                cursor.start_card.min(max_start)
            };
            cursor.start_card = match action {
                TranscriptProjectionAction::Tail => max_start,
                TranscriptProjectionAction::Older => current_start.saturating_sub(shift_cards),
                TranscriptProjectionAction::Newer => {
                    current_start.saturating_add(shift_cards).min(max_start)
                }
                TranscriptProjectionAction::Current => current_start,
            };
            cursor.at_tail = cursor.start_card >= max_start;
            cursor.revision = cursor.revision.saturating_add(1);
            (cursor.start_card, cursor.revision)
        };

        let (cards, runtime_state) = if path.exists() {
            let indexes = self
                .transcript_indexes
                .lock()
                .map_err(|_| anyhow!("transcript index lock poisoned"))?;
            let index = indexes
                .get(&safe_id)
                .ok_or_else(|| anyhow!("transcript index missing after rebuild"))?;
            read_indexed_card_projection(&path, index, start, window_cards, max_bytes)?
        } else {
            (Vec::new(), Vec::new())
        };
        Ok(TranscriptProjection {
            start_card: start,
            end_card: start.saturating_add(window_cards).min(total_cards),
            total_cards,
            window_cards,
            shift_cards,
            at_start: start == 0,
            at_tail: start >= max_start,
            revision,
            cards,
            runtime_state,
            raw_total_count,
        })
    }

    pub fn clear_transcript_projections_for_client(&self, client_id: &str) -> Result<()> {
        self.transcript_projections
            .lock()
            .map_err(|_| anyhow!("transcript projection lock poisoned"))?
            .retain(|(projection_client_id, _), _| projection_client_id != client_id);
        Ok(())
    }

    fn read_transcript_range_unlocked(
        &self,
        conversation_id: &str,
        offset: TranscriptOffset,
        limit: usize,
        max_bytes: usize,
    ) -> Result<TranscriptChunk> {
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
        let max_bytes = max_bytes.max(1);
        self.transcript_total_count_unlocked(&safe_id, &path)?;
        let indexes = self
            .transcript_indexes
            .lock()
            .map_err(|_| anyhow!("transcript index lock poisoned"))?;
        let index = indexes
            .get(&safe_id)
            .ok_or_else(|| anyhow!("transcript index missing after rebuild"))?;
        read_indexed_transcript_projection(&path, offset, limit, max_bytes, index)
    }

    fn transcript_total_count_unlocked(&self, safe_id: &str, path: &PathBuf) -> Result<usize> {
        let file_len = fs::metadata(path)
            .with_context(|| format!("failed to stat transcript {}", path.display()))?
            .len();
        let mut indexes = self
            .transcript_indexes
            .lock()
            .map_err(|_| anyhow!("transcript index lock poisoned"))?;
        let rebuild_index = indexes
            .get(safe_id)
            .map(|index| index.file_len != file_len)
            .unwrap_or(true);
        if rebuild_index {
            indexes.insert(safe_id.to_owned(), build_transcript_line_index(path)?);
        }
        indexes
            .get(safe_id)
            .map(|index| index.line_offsets.len())
            .ok_or_else(|| anyhow!("transcript index missing after rebuild"))
    }

    fn record_appended_transcript_rows_unlocked(
        &self,
        conversation_id: &str,
        previous_file_len: u64,
        row_lengths: &[usize],
        rows: &[Value],
    ) -> Result<()> {
        if row_lengths.len() != rows.len() {
            bail!("transcript append index row length mismatch");
        }
        let safe_id = sanitize_conversation_id(conversation_id);
        let mut indexes = self
            .transcript_indexes
            .lock()
            .map_err(|_| anyhow!("transcript index lock poisoned"))?;
        let Some(index) = indexes.get_mut(&safe_id) else {
            return Ok(());
        };
        if index.file_len != previous_file_len {
            indexes.remove(&safe_id);
            return Ok(());
        }
        let mut next_offset = previous_file_len;
        for (row_len, row) in row_lengths.iter().zip(rows) {
            let line_index = index.line_offsets.len();
            index.line_offsets.push(next_offset);
            index.cards.apply_record(line_index, row);
            next_offset = next_offset.saturating_add(*row_len as u64 + 1);
        }
        index.file_len = next_offset;
        Ok(())
    }

    fn advance_tail_projections_unlocked(&self, conversation_id: &str) -> Result<()> {
        let safe_id = sanitize_conversation_id(conversation_id);
        let total_cards = self
            .transcript_indexes
            .lock()
            .map_err(|_| anyhow!("transcript index lock poisoned"))?
            .get(&safe_id)
            .map(|index| index.cards.total_cards());
        let Some(total_cards) = total_cards else {
            return Ok(());
        };
        let mut projections = self
            .transcript_projections
            .lock()
            .map_err(|_| anyhow!("transcript projection lock poisoned"))?;
        for ((_, projection_conversation_id), cursor) in projections.iter_mut() {
            if projection_conversation_id == &safe_id && cursor.at_tail {
                cursor.start_card = total_cards.saturating_sub(cursor.window_cards);
                cursor.revision = cursor.revision.saturating_add(1);
            }
        }
        Ok(())
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
            extension_id: None,
            thread_id: None,
            provider_session_id: None,
            cwd: None,
            label: None,
            alias: None,
            pinned: false,
            pinned_order: None,
            pending_approvals: JsonMap::new(),
            pending_approvals_revision: 0,
            last_preview: None,
            status: "draft".to_owned(),
            created_at: now.clone(),
            updated_at: now,
            settings: JsonMap::new(),
            draft: None,
            draft_revision: 0,
            draft_selection: None,
            ask_user_msg_counter: 0,
            next_transcript_order_id: 0,
            transcript_line_count: Some(0),
            forked_from_conversation_id: None,
            forked_from_provider_session_id: None,
        }
    }
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub struct CreateConversationRequest {
    pub conversation_id: Option<String>,
    pub title: Option<String>,
    pub agent_type: Option<String>,
    #[serde(default)]
    pub extension_id: Option<String>,
    #[serde(default)]
    pub thread_id: Option<String>,
    #[serde(default)]
    pub cwd: Option<String>,
    #[serde(default)]
    pub label: Option<String>,
    #[serde(default)]
    pub alias: Option<String>,
    #[serde(default)]
    pub pinned: bool,
    #[serde(default)]
    pub settings: JsonMap,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub struct ForkConversationRequest {
    pub source_conversation_id: String,
    #[serde(default)]
    pub conversation_id: Option<String>,
    #[serde(default)]
    pub provider_session_id: String,
    #[serde(default)]
    pub title: Option<String>,
    #[serde(default)]
    pub settings: Option<JsonMap>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct ConversationMeta {
    pub conversation_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub title: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub agent_type: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub extension_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub thread_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub provider_session_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cwd: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub label: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub alias: Option<String>,
    #[serde(default)]
    pub pinned: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub pinned_order: Option<u64>,
    #[serde(default)]
    pub pending_approvals: JsonMap,
    #[serde(default)]
    pub pending_approvals_revision: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_preview: Option<Value>,
    pub status: String,
    pub created_at: String,
    pub updated_at: String,
    #[serde(default)]
    pub settings: JsonMap,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub draft: Option<String>,
    #[serde(default)]
    pub draft_revision: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub draft_selection: Option<ComposerSelection>,
    #[serde(default)]
    pub ask_user_msg_counter: u64,
    #[serde(default)]
    pub next_transcript_order_id: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub transcript_line_count: Option<usize>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub forked_from_conversation_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub forked_from_provider_session_id: Option<String>,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub struct ConversationMetaUpdate {
    #[serde(default)]
    pub settings: Option<JsonMap>,
    #[serde(default)]
    pub thread_id: Option<String>,
    #[serde(default)]
    pub agent_type: Option<String>,
    #[serde(default)]
    pub extension_id: Option<String>,
    #[serde(default)]
    pub title: Option<String>,
    #[serde(default)]
    pub draft: Option<String>,
    #[serde(default)]
    pub cwd: Option<String>,
    #[serde(default)]
    pub label: Option<String>,
    #[serde(default)]
    pub alias: Option<String>,
    #[serde(default)]
    pub pinned: Option<bool>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct ConversationSummary {
    pub conversation_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub title: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub agent_type: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub extension_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub thread_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub provider_session_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cwd: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub label: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub alias: Option<String>,
    #[serde(default)]
    pub pinned: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub pinned_order: Option<u64>,
    #[serde(default)]
    pub pending_approvals: JsonMap,
    #[serde(default)]
    pub pending_approvals_revision: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_preview: Option<Value>,
    pub status: String,
    pub created_at: String,
    pub updated_at: String,
    #[serde(default)]
    pub settings: JsonMap,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub draft: Option<String>,
    #[serde(default)]
    pub draft_revision: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub draft_selection: Option<ComposerSelection>,
    #[serde(default)]
    pub ask_user_msg_counter: u64,
    #[serde(default)]
    pub next_transcript_order_id: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub transcript_line_count: Option<usize>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub forked_from_conversation_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub forked_from_provider_session_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub integrity: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub integrity_error: Option<String>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TranscriptOffset {
    Absolute(usize),
    Latest,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TranscriptProjectionAction {
    Tail,
    Older,
    Newer,
    Current,
}

#[derive(Clone, Debug, PartialEq)]
pub struct TranscriptChunk {
    pub offset: usize,
    pub total_count: usize,
    pub rows: Vec<String>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct TranscriptProjection {
    pub start_card: usize,
    pub end_card: usize,
    pub total_cards: usize,
    pub window_cards: usize,
    pub shift_cards: usize,
    pub at_start: bool,
    pub at_tail: bool,
    pub revision: u64,
    pub cards: Vec<TranscriptCardRecipe>,
    pub runtime_state: Vec<Value>,
    pub raw_total_count: usize,
}

impl From<ConversationMeta> for ConversationSummary {
    fn from(meta: ConversationMeta) -> Self {
        Self {
            conversation_id: meta.conversation_id,
            title: meta.title,
            agent_type: meta.agent_type,
            extension_id: meta.extension_id,
            thread_id: meta.thread_id,
            provider_session_id: meta.provider_session_id,
            cwd: meta.cwd,
            label: meta.label,
            alias: meta.alias,
            pinned: meta.pinned,
            pinned_order: meta.pinned_order,
            pending_approvals: meta.pending_approvals,
            pending_approvals_revision: meta.pending_approvals_revision,
            last_preview: meta.last_preview,
            status: meta.status,
            created_at: meta.created_at,
            updated_at: meta.updated_at,
            settings: meta.settings,
            draft: meta.draft,
            draft_revision: meta.draft_revision,
            draft_selection: meta.draft_selection,
            ask_user_msg_counter: meta.ask_user_msg_counter,
            next_transcript_order_id: meta.next_transcript_order_id,
            transcript_line_count: meta.transcript_line_count,
            forked_from_conversation_id: meta.forked_from_conversation_id,
            forked_from_provider_session_id: meta.forked_from_provider_session_id,
            integrity: None,
            integrity_error: None,
        }
    }
}

impl ConversationSummary {
    fn corrupt(conversation_id: String, error: String, updated_at: String) -> Self {
        Self {
            conversation_id,
            title: None,
            agent_type: None,
            extension_id: None,
            thread_id: None,
            provider_session_id: None,
            cwd: None,
            label: None,
            alias: None,
            pinned: false,
            pinned_order: None,
            pending_approvals: JsonMap::new(),
            pending_approvals_revision: 0,
            last_preview: None,
            status: "corrupt".to_owned(),
            created_at: updated_at.clone(),
            updated_at,
            settings: JsonMap::new(),
            draft: None,
            draft_revision: 0,
            draft_selection: None,
            ask_user_msg_counter: 0,
            next_transcript_order_id: 0,
            transcript_line_count: None,
            forked_from_conversation_id: None,
            forked_from_provider_session_id: None,
            integrity: Some("corrupt".to_owned()),
            integrity_error: Some(error),
        }
    }
}

fn copy_transcript_for_fork(
    source_path: &PathBuf,
    target_dir: &PathBuf,
    target_conversation_id: &str,
) -> Result<(usize, u64)> {
    if !source_path.exists() {
        return Ok((0, 0));
    }
    let source_file = fs::File::open(source_path)
        .with_context(|| format!("failed to open source transcript {}", source_path.display()))?;
    let target_path = target_dir.join("transcript.jsonl");
    let mut target_file = fs::OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(&target_path)
        .with_context(|| format!("failed to create fork transcript {}", target_path.display()))?;
    let reader = BufReader::new(source_file);
    let mut line_count = 0usize;
    let mut next_order_id = 0u64;
    for line in reader.lines() {
        let line = line.context("failed to read source transcript row")?;
        if line.trim().is_empty() {
            continue;
        }
        let mut row: Value =
            serde_json::from_str(&line).context("invalid source transcript JSONL row")?;
        if let Some(object) = row.as_object_mut() {
            object.insert(
                "conversation_id".to_owned(),
                Value::String(target_conversation_id.to_owned()),
            );
            if let Some(order_id) = object.get("order_id").and_then(Value::as_u64) {
                next_order_id = next_order_id.max(order_id.saturating_add(1));
            }
        }
        writeln!(target_file, "{}", serde_json::to_string(&row)?)
            .context("failed to write fork transcript row")?;
        line_count += 1;
    }
    if next_order_id < line_count as u64 {
        next_order_id = line_count as u64;
    }
    Ok((line_count, next_order_id))
}

fn compare_summaries_for_display(
    left: &ConversationSummary,
    right: &ConversationSummary,
) -> std::cmp::Ordering {
    match (left.pinned, right.pinned) {
        (true, false) => return std::cmp::Ordering::Less,
        (false, true) => return std::cmp::Ordering::Greater,
        _ => {}
    }
    if left.pinned && right.pinned {
        let left_order = left.pinned_order.unwrap_or(u64::MAX);
        let right_order = right.pinned_order.unwrap_or(u64::MAX);
        let order = left_order.cmp(&right_order);
        if order != std::cmp::Ordering::Equal {
            return order;
        }
    }
    right.updated_at.cmp(&left.updated_at)
}

fn merge_settings_patch(settings: &mut JsonMap, patch: JsonMap) {
    for (key, value) in patch {
        if value.is_null() || value.as_str().is_some_and(str::is_empty) {
            settings.remove(&key);
        } else {
            settings.insert(key, value);
        }
    }
}

fn set_conversation_extension(meta: &mut ConversationMeta, extension_id: String) {
    if let Some(extension_id) = nonempty_owned(extension_id) {
        meta.extension_id = Some(extension_id.clone());
        meta.agent_type = Some(extension_id.clone());
        set_string_setting(&mut meta.settings, "agent", extension_id);
    }
}

fn sync_meta_from_settings(meta: &mut ConversationMeta) {
    if let Some(agent) = string_setting(&meta.settings, "agent") {
        meta.extension_id = Some(agent.clone());
        meta.agent_type = Some(agent);
    }
    meta.cwd = string_setting(&meta.settings, "cwd");
    meta.label = string_setting(&meta.settings, "label");
    meta.alias = string_setting(&meta.settings, "alias");
}

fn update_meta_from_transcript_entry(meta: &mut ConversationMeta, entry: &Value) {
    let Some(object) = entry.as_object() else {
        return;
    };
    if let Some(thread_id) = first_nonempty_string(
        object,
        &["thread_id", "threadId", "provider_session_id", "session_id"],
    ) {
        meta.thread_id = Some(thread_id.clone());
        meta.provider_session_id = Some(thread_id);
    }

    let role = object
        .get("role")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .trim()
        .to_ascii_lowercase();
    let event_type = object
        .get("type")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .trim()
        .to_ascii_lowercase();

    if let Some(status) = first_nonempty_string(object, &["status"]) {
        meta.status = status;
    } else if role == "error" || event_type == "error" {
        meta.status = "error".to_owned();
    } else if role != "debug_raw" && event_type != "debug_raw" {
        meta.status = "active".to_owned();
    }

    if let Some(text) = preview_text_from_entry(object, &role, &event_type) {
        let source_id = first_nonempty_string(object, &["id", "item_id", "card_id"]);
        let preview_type = if role == "assistant"
            || event_type == "assistant_finalize"
            || event_type == "assistant_end"
        {
            "assistant"
        } else {
            "message"
        };
        let mut preview = Map::new();
        preview.insert("type".to_owned(), Value::String(preview_type.to_owned()));
        preview.insert("text".to_owned(), Value::String(truncate_chars(&text, 400)));
        if let Some(source_id) = source_id {
            preview.insert("source_id".to_owned(), Value::String(source_id));
        }
        meta.last_preview = Some(Value::Object(preview));
    }
}

fn preview_text_from_entry(
    object: &Map<String, Value>,
    role: &str,
    event_type: &str,
) -> Option<String> {
    match (role, event_type) {
        ("assistant", _) | (_, "assistant_finalize" | "assistant_end") => {}
        ("user", _) | (_, "message") => {}
        _ => return None,
    }
    first_nonempty_string(object, &["text", "message"])
}

fn first_nonempty_string(object: &Map<String, Value>, keys: &[&str]) -> Option<String> {
    keys.iter()
        .filter_map(|key| object.get(*key).and_then(Value::as_str))
        .map(str::trim)
        .find(|value| !value.is_empty())
        .map(ToOwned::to_owned)
}

fn truncate_chars(text: &str, max_chars: usize) -> String {
    text.chars().take(max_chars).collect()
}

fn set_string_setting(settings: &mut JsonMap, key: &str, value: String) {
    if let Some(value) = nonempty_owned(value) {
        settings.insert(key.to_owned(), Value::String(value));
    }
}

fn string_setting(settings: &JsonMap, key: &str) -> Option<String> {
    settings
        .get(key)
        .and_then(Value::as_str)
        .and_then(|value| nonempty_owned(value.to_owned()))
}

fn nonempty_owned(value: String) -> Option<String> {
    let value = value.trim().to_owned();
    if value.is_empty() { None } else { Some(value) }
}

fn read_meta(path: &PathBuf) -> Result<ConversationMeta> {
    let raw = fs::read_to_string(path)
        .with_context(|| format!("failed to read conversation meta {}", path.display()))?;
    serde_json::from_str(&raw)
        .with_context(|| format!("invalid conversation meta {}", path.display()))
}

fn build_transcript_line_index(path: &PathBuf) -> Result<TranscriptLineIndex> {
    let file = fs::File::open(path)
        .with_context(|| format!("failed to open transcript {}", path.display()))?;
    let file_len = file.metadata()?.len();
    let mut reader = BufReader::new(file);
    let mut line_offsets = Vec::new();
    let mut cards = TranscriptCardIndex::default();
    let mut line = String::new();
    loop {
        let line_offset = reader.stream_position()?;
        line.clear();
        if reader
            .read_line(&mut line)
            .with_context(|| format!("failed to read transcript {}", path.display()))?
            == 0
        {
            break;
        }
        if line.trim().is_empty() {
            continue;
        }
        let record = serde_json::from_str::<Value>(line.trim_end_matches(['\r', '\n']))
            .with_context(|| format!("invalid transcript JSONL row in {}", path.display()))?;
        cards.apply_record(line_offsets.len(), &record);
        line_offsets.push(line_offset);
    }
    Ok(TranscriptLineIndex {
        file_len,
        line_offsets,
        cards,
    })
}

fn read_indexed_card_projection(
    path: &PathBuf,
    index: &TranscriptLineIndex,
    start_card: usize,
    window_cards: usize,
    max_bytes: usize,
) -> Result<(Vec<TranscriptCardRecipe>, Vec<Value>)> {
    let selection = index.cards.select(start_card, window_cards);
    let mut reader = BufReader::new(
        fs::File::open(path)
            .with_context(|| format!("failed to open transcript {}", path.display()))?,
    );
    let mut used_bytes = 0usize;
    let mut cards = Vec::with_capacity(selection.card_indices.len());

    for card_index in selection.card_indices {
        let indexed = index
            .cards
            .indexed_recipe(card_index)
            .ok_or_else(|| anyhow!("transcript card index missing card {card_index}"))?;
        let mut events = Vec::with_capacity(indexed.events.len());
        for event in indexed.events {
            let (value, operation) = match event {
                IndexedProjectionEvent::Line {
                    line_index,
                    operation,
                } => (
                    read_indexed_json_value(path, &mut reader, index, line_index)?,
                    operation,
                ),
                IndexedProjectionEvent::Synthetic { value, operation } => (value, operation),
            };
            let value = apply_projection_metadata(
                value,
                &indexed.card_id,
                indexed.card_index,
                operation,
                indexed.parent_card_id.as_deref(),
            );
            used_bytes = used_bytes.saturating_add(serde_json::to_vec(&value)?.len());
            if used_bytes > max_bytes {
                bail!(
                    "card projection exceeds max_bytes ({used_bytes} > {max_bytes}); increase the projection byte budget"
                );
            }
            events.push(value);
        }
        cards.push(TranscriptCardRecipe {
            card_id: indexed.card_id,
            card_index: indexed.card_index,
            family: indexed.family,
            scope: "durable",
            parent_card_id: indexed.parent_card_id,
            events,
        });
    }

    let mut runtime_state = Vec::with_capacity(selection.runtime_state_lines.len());
    for line_index in selection.runtime_state_lines {
        let value = read_indexed_json_value(path, &mut reader, index, line_index)?;
        used_bytes = used_bytes.saturating_add(serde_json::to_vec(&value)?.len());
        if used_bytes > max_bytes {
            bail!(
                "card projection exceeds max_bytes ({used_bytes} > {max_bytes}); increase the projection byte budget"
            );
        }
        runtime_state.push(value);
    }
    Ok((cards, runtime_state))
}

fn read_indexed_json_value(
    path: &PathBuf,
    reader: &mut BufReader<fs::File>,
    index: &TranscriptLineIndex,
    line_index: usize,
) -> Result<Value> {
    let offset = *index
        .line_offsets
        .get(line_index)
        .ok_or_else(|| anyhow!("transcript line index out of bounds: {line_index}"))?;
    reader
        .seek(SeekFrom::Start(offset))
        .with_context(|| format!("failed to seek transcript {}", path.display()))?;
    let mut line = String::new();
    reader
        .read_line(&mut line)
        .with_context(|| format!("failed to read transcript {}", path.display()))?;
    serde_json::from_str(line.trim_end_matches(['\r', '\n']))
        .with_context(|| format!("invalid transcript JSONL row in {}", path.display()))
}

fn read_indexed_transcript_projection(
    path: &PathBuf,
    offset: TranscriptOffset,
    limit: usize,
    max_bytes: usize,
    index: &TranscriptLineIndex,
) -> Result<TranscriptChunk> {
    let total_count = index.line_offsets.len();
    let start = match offset {
        TranscriptOffset::Absolute(offset) => offset.min(total_count),
        TranscriptOffset::Latest => total_count.saturating_sub(limit),
    };
    if start >= total_count {
        return Ok(TranscriptChunk {
            offset: start,
            total_count,
            rows: Vec::new(),
        });
    }
    let mut file = fs::File::open(path)
        .with_context(|| format!("failed to open transcript {}", path.display()))?;
    file.seek(SeekFrom::Start(index.line_offsets[start]))
        .with_context(|| format!("failed to seek transcript {}", path.display()))?;
    let mut reader = BufReader::new(file);
    let mut rows = Vec::with_capacity(limit.min(total_count - start));
    let mut used_bytes = 0usize;
    let mut line = String::new();
    while rows.len() < limit && start + rows.len() < total_count {
        line.clear();
        if reader
            .read_line(&mut line)
            .with_context(|| format!("failed to read transcript {}", path.display()))?
            == 0
        {
            break;
        }
        let row = line.trim_end_matches(['\r', '\n']);
        if row.trim().is_empty() {
            continue;
        }
        let row_bytes = row.len().saturating_add(1);
        if !rows.is_empty() && used_bytes.saturating_add(row_bytes) > max_bytes {
            break;
        }
        used_bytes = used_bytes.saturating_add(row_bytes);
        rows.push(row.to_owned());
    }
    Ok(TranscriptChunk {
        offset: start,
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
        if !line.trim().is_empty() {
            total_count += 1;
        }
    }
    Ok(total_count)
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

fn stamp_pending_approval_revision(descriptor: &mut JsonMap, revision: u64) {
    descriptor.insert(
        "pending_approvals_revision".to_owned(),
        Value::Number(revision.into()),
    );
    if let Some(render_event) = descriptor
        .get_mut("render_event")
        .and_then(Value::as_object_mut)
    {
        render_event.insert(
            "pending_approvals_revision".to_owned(),
            Value::Number(revision.into()),
        );
    }
}

fn utc_ts() -> String {
    format!("unix_ms:{}", unix_millis())
}

fn file_modified_ts(path: &PathBuf) -> String {
    path.metadata()
        .and_then(|metadata| metadata.modified())
        .ok()
        .and_then(|modified| modified.duration_since(UNIX_EPOCH).ok())
        .map(|duration| format!("unix_ms:{}", duration.as_millis()))
        .unwrap_or_else(utc_ts)
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
                agent_type: Some("sample-ext".to_owned()),
                settings: JsonMap::new(),
                ..CreateConversationRequest::default()
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
    fn draft_updates_increment_revision_and_persist_selection() {
        let root = std::env::temp_dir().join(format!("als-rs-store-draft-test-{}", unix_millis()));
        let store = ConversationStore::new(root.clone());
        store
            .create(CreateConversationRequest {
                conversation_id: Some("draft-test".to_owned()),
                ..CreateConversationRequest::default()
            })
            .unwrap();

        let first = store
            .set_draft(
                "draft-test",
                "hello".to_owned(),
                Some(ComposerSelection {
                    anchor: 2,
                    focus: 2,
                }),
            )
            .unwrap();
        let second = store
            .set_draft(
                "draft-test",
                "hello world".to_owned(),
                Some(ComposerSelection {
                    anchor: 11,
                    focus: 11,
                }),
            )
            .unwrap();

        assert_eq!(first.draft_revision, 1);
        assert_eq!(second.draft_revision, 2);
        assert_eq!(second.draft.as_deref(), Some("hello world"));
        assert_eq!(second.draft_selection.unwrap().focus, 11);

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn list_preserves_healthy_conversations_and_reports_corrupt_metadata() {
        let root =
            std::env::temp_dir().join(format!("als-rs-store-corrupt-list-test-{}", unix_millis()));
        let store = ConversationStore::new(root.clone());
        store
            .create(CreateConversationRequest {
                conversation_id: Some("healthy-conversation".to_owned()),
                ..CreateConversationRequest::default()
            })
            .unwrap();
        let corrupt_path = root
            .join("conversations")
            .join("corrupt-conversation")
            .join("meta.json");
        fs::create_dir_all(corrupt_path.parent().unwrap()).unwrap();
        fs::write(&corrupt_path, b"\0\0\0\0").unwrap();

        let summaries = store.list().unwrap();
        assert_eq!(summaries.len(), 2);
        assert!(
            summaries
                .iter()
                .any(|summary| summary.conversation_id == "healthy-conversation"
                    && summary.integrity.is_none())
        );
        let corrupt = summaries
            .iter()
            .find(|summary| summary.conversation_id == "corrupt-conversation")
            .unwrap();
        assert_eq!(corrupt.status, "corrupt");
        assert_eq!(corrupt.integrity.as_deref(), Some("corrupt"));
        assert!(
            corrupt
                .integrity_error
                .as_deref()
                .is_some_and(|error| error.contains("invalid conversation meta"))
        );
        assert_eq!(fs::read(&corrupt_path).unwrap(), b"\0\0\0\0");

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn pending_approval_mutations_advance_and_stamp_revision() {
        let root = std::env::temp_dir().join(format!(
            "als-rs-store-approval-revision-test-{}",
            unix_millis()
        ));
        let store = ConversationStore::new(root.clone());
        store
            .create(CreateConversationRequest {
                conversation_id: Some("approval-revision-test".to_owned()),
                ..CreateConversationRequest::default()
            })
            .unwrap();

        let first = store
            .upsert_pending_approval(
                "approval-revision-test",
                "request-a",
                json!({
                    "requestor_id": "approval-revision-test",
                    "request_method": "agent-pty/ask-user",
                    "render_event": {"request_id": "request-a"},
                })
                .as_object()
                .cloned()
                .unwrap(),
            )
            .unwrap();
        assert_eq!(first.pending_approvals_revision, 1);
        assert_eq!(
            first.pending_approvals["request-a"]["pending_approvals_revision"],
            1
        );
        assert_eq!(
            first.pending_approvals["request-a"]["render_event"]["pending_approvals_revision"],
            1
        );

        let (second, replaced) = store
            .replace_pending_approval_for_requestor(
                "approval-revision-test",
                "request-b",
                "approval-revision-test",
                "agent-pty/ask-user",
                json!({
                    "requestor_id": "approval-revision-test",
                    "request_method": "agent-pty/ask-user",
                    "render_event": {"request_id": "request-b"},
                })
                .as_object()
                .cloned()
                .unwrap(),
            )
            .unwrap();
        assert_eq!(replaced.len(), 1);
        assert_eq!(second.pending_approvals_revision, 2);
        assert_eq!(
            second.pending_approvals["request-b"]["pending_approvals_revision"],
            2
        );

        assert!(
            store
                .remove_pending_approval("approval-revision-test", "request-b")
                .unwrap()
                .is_some()
        );
        assert_eq!(
            store
                .load_meta("approval-revision-test")
                .unwrap()
                .pending_approvals_revision,
            3
        );

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
            .read_transcript_range("latest-test", TranscriptOffset::Latest, 2, usize::MAX)
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
            .read_transcript_range("latest-test", TranscriptOffset::Absolute(1), 2, usize::MAX)
            .unwrap();
        assert_eq!(middle.total_count, 5);
        assert_eq!(middle.offset, 1);
        assert_eq!(middle.rows.len(), 2);
        assert!(middle.rows[0].contains("\"text\":\"row 1\""));
        assert!(middle.rows[1].contains("\"text\":\"row 2\""));

        let byte_bounded = store
            .read_transcript_range("latest-test", TranscriptOffset::Absolute(1), 4, 1)
            .unwrap();
        assert_eq!(byte_bounded.offset, 1);
        assert_eq!(byte_bounded.total_count, 5);
        assert_eq!(byte_bounded.rows.len(), 1);
        assert!(byte_bounded.rows[0].contains("\"text\":\"row 1\""));

        store
            .append_transcript("latest-test", json!({"role": "user", "text": "row 5"}))
            .unwrap();
        let latest_after_append = store
            .read_transcript_range("latest-test", TranscriptOffset::Latest, 2, usize::MAX)
            .unwrap();
        assert_eq!(latest_after_append.total_count, 6);
        assert!(latest_after_append.rows[0].contains("\"text\":\"row 4\""));
        assert!(latest_after_append.rows[1].contains("\"text\":\"row 5\""));

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn transcript_projection_position_is_server_owned_per_client() {
        let root =
            std::env::temp_dir().join(format!("als-rs-store-projection-test-{}", unix_millis()));
        let store = ConversationStore::new(root.clone());
        store
            .create(CreateConversationRequest {
                conversation_id: Some("projection-test".to_owned()),
                ..CreateConversationRequest::default()
            })
            .unwrap();
        for idx in 0..10 {
            store
                .append_transcript(
                    "projection-test",
                    json!({"role": "user", "text": format!("row {idx}")}),
                )
                .unwrap();
        }

        let tail = store
            .project_transcript(
                "client-a",
                "projection-test",
                TranscriptProjectionAction::Tail,
                4,
                1,
                usize::MAX,
            )
            .unwrap();
        assert_eq!(tail.start_card, 6);
        assert!(tail.at_tail);
        assert_eq!(tail.cards[0].events[0]["text"], "row 6");

        let older = store
            .project_transcript(
                "client-a",
                "projection-test",
                TranscriptProjectionAction::Older,
                4,
                1,
                usize::MAX,
            )
            .unwrap();
        assert_eq!(older.start_card, 5);
        assert!(!older.at_tail);

        let independent_tail = store
            .project_transcript(
                "client-b",
                "projection-test",
                TranscriptProjectionAction::Current,
                4,
                1,
                usize::MAX,
            )
            .unwrap();
        assert_eq!(independent_tail.start_card, 6);
        assert!(independent_tail.at_tail);

        store
            .append_transcript("projection-test", json!({"role": "user", "text": "row 10"}))
            .unwrap();
        let detached_current = store
            .project_transcript(
                "client-a",
                "projection-test",
                TranscriptProjectionAction::Current,
                4,
                1,
                usize::MAX,
            )
            .unwrap();
        assert_eq!(detached_current.start_card, 5);
        let advanced_tail = store
            .project_transcript(
                "client-b",
                "projection-test",
                TranscriptProjectionAction::Current,
                4,
                1,
                usize::MAX,
            )
            .unwrap();
        assert_eq!(advanced_tail.start_card, 7);
        assert!(advanced_tail.at_tail);

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn transcript_projection_counts_cards_instead_of_jsonl_records() {
        let root =
            std::env::temp_dir().join(format!("als-rs-store-card-window-test-{}", unix_millis()));
        let store = ConversationStore::new(root.clone());
        store
            .create(CreateConversationRequest {
                conversation_id: Some("card-window-test".to_owned()),
                ..CreateConversationRequest::default()
            })
            .unwrap();
        for idx in 0..120 {
            store
                .append_transcript_batch(
                    "card-window-test",
                    vec![
                        json!({"role":"user","id":format!("message-{idx}"),"text":format!("message {idx}")}),
                        json!({"role":"token_usage","total":idx,"context_window":200_000}),
                    ],
                )
                .unwrap();
        }

        let tail = store
            .project_transcript(
                "client",
                "card-window-test",
                TranscriptProjectionAction::Tail,
                75,
                20,
                usize::MAX,
            )
            .unwrap();
        assert_eq!(tail.raw_total_count, 240);
        assert_eq!(tail.total_cards, 120);
        assert_eq!(tail.start_card, 45);
        assert_eq!(tail.end_card, 120);
        assert_eq!(tail.cards.len(), 75);
        assert_eq!(tail.cards[0].events[0]["text"], "message 45");
        assert_eq!(tail.runtime_state.len(), 1);
        assert_eq!(tail.runtime_state[0]["total"], 119);

        let older = store
            .project_transcript(
                "client",
                "card-window-test",
                TranscriptProjectionAction::Older,
                75,
                20,
                usize::MAX,
            )
            .unwrap();
        assert_eq!(older.start_card, 25);
        assert_eq!(older.end_card, 100);
        assert_eq!(older.cards.len(), 75);
        assert!(!older.at_tail);

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn append_transcript_returns_card_metadata_without_persisting_it() {
        let root = std::env::temp_dir().join(format!(
            "als-rs-store-live-card-metadata-test-{}",
            unix_millis()
        ));
        let store = ConversationStore::new(root.clone());
        store
            .create(CreateConversationRequest {
                conversation_id: Some("live-card-metadata".to_owned()),
                ..CreateConversationRequest::default()
            })
            .unwrap();

        let emitted = store
            .append_transcript("live-card-metadata", json!({"role":"user","text":"hello"}))
            .unwrap();
        assert_eq!(emitted["projection_card_index"], 0);
        assert_eq!(emitted["projection_card_op"], "create");
        assert_eq!(emitted["projection_card_id"], "user:line-0:0");
        assert!(emitted.get("card_id").is_none());

        let persisted = store.read_transcript("live-card-metadata").unwrap();
        assert_eq!(persisted.len(), 1);
        assert!(persisted[0].get("projection_card_id").is_none());
        assert!(persisted[0].get("card_id").is_none());

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn forks_conversation_meta_and_rewrites_transcript_conversation_id() {
        let root = std::env::temp_dir().join(format!("als-rs-store-fork-test-{}", unix_millis()));
        let store = ConversationStore::new(root.clone());
        let mut settings = JsonMap::new();
        settings.insert("agent".to_owned(), json!("codex-ext"));
        settings.insert("cwd".to_owned(), json!("/repo/project"));
        store
            .create(CreateConversationRequest {
                conversation_id: Some("source-fork".to_owned()),
                title: Some("Original".to_owned()),
                thread_id: Some("thread-source".to_owned()),
                settings,
                ..CreateConversationRequest::default()
            })
            .unwrap();
        store
            .append_transcript(
                "source-fork",
                json!({"role": "user", "text": "hi", "conversation_id": "source-fork"}),
            )
            .unwrap();

        let forked = store
            .fork_from(ForkConversationRequest {
                source_conversation_id: "source-fork".to_owned(),
                conversation_id: Some("target-fork".to_owned()),
                provider_session_id: "thread-target".to_owned(),
                ..ForkConversationRequest::default()
            })
            .unwrap();

        assert_eq!(forked.conversation_id, "target-fork");
        assert_eq!(forked.thread_id.as_deref(), Some("thread-target"));
        assert_eq!(
            forked.forked_from_conversation_id.as_deref(),
            Some("source-fork")
        );
        assert_eq!(
            forked.forked_from_provider_session_id.as_deref(),
            Some("thread-source")
        );
        assert_eq!(forked.pending_approvals.len(), 0);
        assert_eq!(forked.transcript_line_count, Some(1));
        assert_eq!(forked.next_transcript_order_id, 1);
        let rows = store.read_transcript("target-fork").unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0]["conversation_id"], "target-fork");

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn persists_card_metadata_and_pin_order() {
        let root = std::env::temp_dir().join(format!("als-rs-store-meta-test-{}", unix_millis()));
        let store = ConversationStore::new(root.clone());
        let mut settings = JsonMap::new();
        settings.insert("agent".to_owned(), json!("other-ext"));
        settings.insert("cwd".to_owned(), json!("/repo/project"));
        settings.insert("label".to_owned(), json!("Project chat"));
        settings.insert("alias".to_owned(), json!("agent one"));

        let meta = store
            .create(CreateConversationRequest {
                conversation_id: Some("meta-a".to_owned()),
                settings,
                ..CreateConversationRequest::default()
            })
            .unwrap();
        assert_eq!(meta.extension_id.as_deref(), Some("other-ext"));
        assert_eq!(meta.agent_type.as_deref(), Some("other-ext"));
        assert_eq!(meta.cwd.as_deref(), Some("/repo/project"));
        assert_eq!(meta.label.as_deref(), Some("Project chat"));
        assert_eq!(meta.alias.as_deref(), Some("agent one"));

        store
            .create(CreateConversationRequest {
                conversation_id: Some("meta-b".to_owned()),
                extension_id: Some("sample-ext".to_owned()),
                ..CreateConversationRequest::default()
            })
            .unwrap();
        let pinned = store
            .set_pinned_conversations(vec![
                "meta-b".to_owned(),
                "missing".to_owned(),
                "meta-a".to_owned(),
                "meta-b".to_owned(),
            ])
            .unwrap();
        assert_eq!(pinned, vec!["meta-b", "meta-a"]);

        let list = store.list().unwrap();
        assert_eq!(list[0].conversation_id, "meta-b");
        assert_eq!(list[0].pinned_order, Some(0));
        assert_eq!(list[1].conversation_id, "meta-a");
        assert_eq!(list[1].pinned_order, Some(1));
        assert_eq!(list[1].settings["agent"], "other-ext");

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn append_transcript_updates_card_state() {
        let root = std::env::temp_dir().join(format!("als-rs-store-card-test-{}", unix_millis()));
        let store = ConversationStore::new(root.clone());
        store
            .create(CreateConversationRequest {
                conversation_id: Some("card-state".to_owned()),
                ..CreateConversationRequest::default()
            })
            .unwrap();

        store
            .append_transcript(
                "card-state",
                json!({
                    "role": "assistant",
                    "text": "Final answer preview",
                    "thread_id": "thread-123"
                }),
            )
            .unwrap();
        let meta = store.load_meta("card-state").unwrap();
        assert_eq!(meta.status, "active");
        assert_eq!(meta.thread_id.as_deref(), Some("thread-123"));
        assert_eq!(meta.provider_session_id.as_deref(), Some("thread-123"));
        assert_eq!(
            meta.last_preview.as_ref().unwrap()["text"],
            "Final answer preview"
        );

        store
            .append_transcript("card-state", json!({"role": "status", "status": "success"}))
            .unwrap();
        let meta = store.load_meta("card-state").unwrap();
        assert_eq!(meta.status, "success");

        let _ = fs::remove_dir_all(root);
    }
}
