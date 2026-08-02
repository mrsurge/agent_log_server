use crate::conversation_store::{
    ConversationStore, TranscriptProjection, TranscriptProjectionAction,
    TranscriptProjectionTransfer,
};
use anyhow::{Result, anyhow};
use serde::Serialize;
use serde_json::Value;
use std::{
    collections::{HashMap, VecDeque},
    sync::{Arc, Mutex},
};

const MAX_PROJECTED_ITEMS: usize = 96;
const MAX_PROJECTED_TEXT_BYTES: usize = 512 * 1024;
const MAX_DURABLE_LINEAGES: usize = 256;

#[derive(Clone, Default)]
pub struct TurnProjectionStore {
    conversations: Arc<Mutex<HashMap<String, Arc<Mutex<ConversationTurnProjection>>>>>,
}

#[derive(Clone, Debug, Default)]
struct ConversationTurnProjection {
    generation: u64,
    revision: u64,
    next_sequence: u64,
    active_turn_id: Option<String>,
    items: Vec<ProjectedTurnItem>,
    durable_lineages: VecDeque<String>,
    truncated: bool,
}

#[derive(Clone, Debug)]
struct ProjectedTurnItem {
    key: String,
    lineage: String,
    family: String,
    source_id: String,
    turn_id: Option<String>,
    subagent_id: Option<String>,
    sequence: u64,
    events: Vec<Value>,
}

#[derive(Clone, Debug, Default, PartialEq, Serialize)]
pub struct TurnProjectionSnapshot {
    pub generation: u64,
    pub revision: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub turn_id: Option<String>,
    pub items: Vec<TurnProjectionItemSnapshot>,
    pub truncated: bool,
}

#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct TurnProjectionItemSnapshot {
    pub key: String,
    pub card_id: String,
    pub family: String,
    pub scope: &'static str,
    pub source_id: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub turn_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub subagent_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub parent_card_id: Option<String>,
    pub sequence: u64,
    pub events: Vec<Value>,
}

#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct TurnProjectionChange {
    pub conversation_id: String,
    pub generation: u64,
    pub revision: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub turn_id: Option<String>,
    pub item_count: usize,
    pub reason: String,
    pub truncated: bool,
}

#[derive(Clone, Debug)]
struct ProjectionIdentity {
    key: String,
    lineage: String,
    family: String,
    source_id: String,
    turn_id: Option<String>,
    subagent_id: Option<String>,
}

impl TurnProjectionStore {
    pub fn begin_send(&self, conversation_id: &str) -> Result<TurnProjectionChange> {
        let cell = self.cell(conversation_id)?;
        let mut state = cell
            .lock()
            .map_err(|_| anyhow!("turn projection lock poisoned"))?;
        state.generation = state.generation.saturating_add(1);
        state.revision = state.revision.saturating_add(1);
        state.next_sequence = 0;
        state.active_turn_id = None;
        state.items.clear();
        state.durable_lineages.clear();
        state.truncated = false;
        Ok(change_for(conversation_id, &state, "send_started"))
    }

    pub fn reduce_live(
        &self,
        conversation_id: &str,
        event: &Value,
    ) -> Result<Option<TurnProjectionChange>> {
        let Some(event_type) = event_type(event) else {
            return Ok(None);
        };
        if !is_projection_event(&event_type) {
            return Ok(None);
        }
        let cell = self.cell(conversation_id)?;
        let mut state = cell
            .lock()
            .map_err(|_| anyhow!("turn projection lock poisoned"))?;

        if event_type == "status" && terminal_status_event(event) {
            if state.items.is_empty() {
                return Ok(None);
            }
            state.items.clear();
            state.revision = state.revision.saturating_add(1);
            return Ok(Some(change_for(conversation_id, &state, "turn_completed")));
        }

        let Some(identity) = live_identity(&state, event, &event_type) else {
            return Ok(None);
        };
        rotate_turn_if_needed(&mut state, identity.turn_id.as_deref());
        if state.durable_lineages.contains(&identity.lineage) {
            return Ok(None);
        }

        if identity.family == "tool"
            && state
                .items
                .iter()
                .any(|item| item.lineage == identity.lineage && item.family != identity.family)
        {
            return Ok(None);
        }

        let replacement_sequence = (identity.family != "tool")
            .then(|| {
                state
                    .items
                    .iter()
                    .filter(|item| item.lineage == identity.lineage && item.family == "tool")
                    .map(|item| item.sequence)
                    .min()
            })
            .flatten();
        if replacement_sequence.is_some() {
            state
                .items
                .retain(|item| !(item.lineage == identity.lineage && item.family == "tool"));
        }

        let existing_index = state.items.iter().position(|item| item.key == identity.key);
        let changed = if let Some(index) = existing_index {
            reduce_item(&mut state.items[index], event, &event_type)
        } else {
            if state.items.len() >= MAX_PROJECTED_ITEMS {
                state.items.remove(0);
                state.truncated = true;
            }
            let sequence = replacement_sequence.unwrap_or_else(|| {
                let sequence = state.next_sequence;
                state.next_sequence = state.next_sequence.saturating_add(1);
                sequence
            });
            let mut item = ProjectedTurnItem {
                key: identity.key,
                lineage: identity.lineage,
                family: identity.family,
                source_id: identity.source_id,
                turn_id: identity.turn_id,
                subagent_id: identity.subagent_id,
                sequence,
                events: Vec::new(),
            };
            item.events = initial_recipe(event, &event_type)
                .into_iter()
                .map(|event| with_projection_metadata(event, &item.key, item.sequence))
                .collect();
            state.items.push(item);
            true
        };
        if !changed {
            return Ok(None);
        }
        state.revision = state.revision.saturating_add(1);
        Ok(Some(change_for(conversation_id, &state, "live_mutation")))
    }

    pub fn decorate_live_event(&self, conversation_id: &str, event: &mut Value) -> Result<()> {
        let Some(event_type) = event_type(event) else {
            return Ok(());
        };
        let cell = self.cell(conversation_id)?;
        let state = cell
            .lock()
            .map_err(|_| anyhow!("turn projection lock poisoned"))?;
        let Some(identity) = live_identity(&state, event, &event_type) else {
            return Ok(());
        };
        let Some(item) = state.items.iter().find(|item| item.key == identity.key) else {
            return Ok(());
        };
        let parent_card_id = item.subagent_id.as_ref().map(|subagent_id| {
            state
                .items
                .iter()
                .find(|candidate| {
                    candidate.family == "subagent" && candidate.source_id == *subagent_id
                })
                .map(|candidate| active_card_id(&candidate.key))
                .unwrap_or_else(|| format!("subagent:{subagent_id}"))
        });
        *event = with_snapshot_card_metadata(
            event.clone(),
            &active_card_id(&item.key),
            item.sequence,
            "update",
            parent_card_id.as_deref(),
        );
        Ok(())
    }

    pub fn append_transcript_and_acknowledge(
        &self,
        conversations: &ConversationStore,
        conversation_id: &str,
        entry: Value,
    ) -> Result<(Value, Option<TurnProjectionChange>)> {
        let cell = self.cell(conversation_id)?;
        let mut state = cell
            .lock()
            .map_err(|_| anyhow!("turn projection lock poisoned"))?;
        let appended = conversations.append_transcript(conversation_id, entry)?;
        let changed = acknowledge_transcript(&mut state, &appended);
        let change = changed.then(|| change_for(conversation_id, &state, "durable_handoff"));
        Ok((appended, change))
    }

    #[allow(clippy::too_many_arguments)]
    pub fn project_transcript_with_live(
        &self,
        conversations: &ConversationStore,
        client_id: &str,
        conversation_id: &str,
        action: TranscriptProjectionAction,
        window_cards: usize,
        shift_cards: usize,
        max_bytes: usize,
    ) -> Result<(TranscriptProjection, TurnProjectionSnapshot)> {
        let cell = self.cell(conversation_id)?;
        let state = cell
            .lock()
            .map_err(|_| anyhow!("turn projection lock poisoned"))?;
        let transcript = conversations.project_transcript(
            client_id,
            conversation_id,
            action,
            window_cards,
            shift_cards,
            max_bytes,
        )?;
        Ok((transcript, snapshot_from(&state)))
    }

    #[allow(clippy::too_many_arguments)]
    pub fn project_transcript_transfer_with_live(
        &self,
        conversations: &ConversationStore,
        client_id: &str,
        conversation_id: &str,
        action: TranscriptProjectionAction,
        window_cards: usize,
        shift_cards: usize,
        max_bytes: usize,
        requested_start: Option<usize>,
        known_cards: &HashMap<String, u64>,
    ) -> Result<(TranscriptProjectionTransfer, TurnProjectionSnapshot)> {
        let cell = self.cell(conversation_id)?;
        let state = cell
            .lock()
            .map_err(|_| anyhow!("turn projection lock poisoned"))?;
        let transcript = conversations.project_transcript_transfer(
            client_id,
            conversation_id,
            action,
            window_cards,
            shift_cards,
            max_bytes,
            requested_start,
            known_cards,
        )?;
        Ok((transcript, snapshot_from(&state)))
    }

    pub fn snapshot(&self, conversation_id: &str) -> Result<TurnProjectionSnapshot> {
        let cell = self.cell(conversation_id)?;
        let state = cell
            .lock()
            .map_err(|_| anyhow!("turn projection lock poisoned"))?;
        Ok(snapshot_from(&state))
    }

    pub fn remove(&self, conversation_id: &str) -> Result<()> {
        self.conversations
            .lock()
            .map_err(|_| anyhow!("turn projection registry lock poisoned"))?
            .remove(conversation_id);
        Ok(())
    }

    fn cell(&self, conversation_id: &str) -> Result<Arc<Mutex<ConversationTurnProjection>>> {
        let mut conversations = self
            .conversations
            .lock()
            .map_err(|_| anyhow!("turn projection registry lock poisoned"))?;
        Ok(conversations
            .entry(conversation_id.to_owned())
            .or_insert_with(|| Arc::new(Mutex::new(ConversationTurnProjection::default())))
            .clone())
    }
}

fn change_for(
    conversation_id: &str,
    state: &ConversationTurnProjection,
    reason: &str,
) -> TurnProjectionChange {
    TurnProjectionChange {
        conversation_id: conversation_id.to_owned(),
        generation: state.generation,
        revision: state.revision,
        turn_id: state.active_turn_id.clone(),
        item_count: state.items.len(),
        reason: reason.to_owned(),
        truncated: state.truncated,
    }
}

fn snapshot_from(state: &ConversationTurnProjection) -> TurnProjectionSnapshot {
    let subagent_cards = state
        .items
        .iter()
        .filter(|item| item.family == "subagent")
        .map(|item| (item.source_id.clone(), active_card_id(&item.key)))
        .collect::<HashMap<_, _>>();
    TurnProjectionSnapshot {
        generation: state.generation,
        revision: state.revision,
        turn_id: state.active_turn_id.clone(),
        items: state
            .items
            .iter()
            .map(|item| {
                let card_id = active_card_id(&item.key);
                let parent_card_id = item.subagent_id.as_ref().map(|subagent_id| {
                    subagent_cards
                        .get(subagent_id)
                        .cloned()
                        .unwrap_or_else(|| format!("subagent:{subagent_id}"))
                });
                let events = item
                    .events
                    .iter()
                    .enumerate()
                    .map(|(index, event)| {
                        with_snapshot_card_metadata(
                            event.clone(),
                            &card_id,
                            item.sequence,
                            if index == 0 { "create" } else { "update" },
                            parent_card_id.as_deref(),
                        )
                    })
                    .collect();
                TurnProjectionItemSnapshot {
                    key: item.key.clone(),
                    card_id,
                    family: item.family.clone(),
                    scope: "active",
                    source_id: item.source_id.clone(),
                    turn_id: item.turn_id.clone(),
                    subagent_id: item.subagent_id.clone(),
                    parent_card_id,
                    sequence: item.sequence,
                    events,
                }
            })
            .collect(),
        truncated: state.truncated,
    }
}

fn active_card_id(key: &str) -> String {
    format!("active:{key}")
}

fn with_snapshot_card_metadata(
    mut event: Value,
    card_id: &str,
    card_index: u64,
    operation: &str,
    parent_card_id: Option<&str>,
) -> Value {
    if let Some(object) = event.as_object_mut() {
        object.insert(
            "projection_card_id".to_owned(),
            Value::String(card_id.to_owned()),
        );
        object.insert(
            "projection_card_index".to_owned(),
            Value::Number(card_index.into()),
        );
        object.insert(
            "projection_card_op".to_owned(),
            Value::String(operation.to_owned()),
        );
        object.insert(
            "projection_card_scope".to_owned(),
            Value::String("active".to_owned()),
        );
        if let Some(parent_card_id) = parent_card_id {
            object.insert(
                "projection_parent_card_id".to_owned(),
                Value::String(parent_card_id.to_owned()),
            );
        }
    }
    event
}

fn rotate_turn_if_needed(state: &mut ConversationTurnProjection, turn_id: Option<&str>) {
    let Some(turn_id) = turn_id.filter(|value| !value.is_empty()) else {
        return;
    };
    if state.active_turn_id.as_deref() == Some(turn_id) {
        return;
    }
    if state.active_turn_id.is_some() {
        state.generation = state.generation.saturating_add(1);
        state.next_sequence = 0;
        state.items.clear();
        state.durable_lineages.clear();
        state.truncated = false;
    }
    state.active_turn_id = Some(turn_id.to_owned());
}

fn reduce_item(item: &mut ProjectedTurnItem, event: &Value, kind: &str) -> bool {
    let projection_key = item.key.clone();
    let projection_sequence = item.sequence;
    if is_final_event(kind) {
        item.events.clear();
        item.events.push(with_projection_metadata(
            event.clone(),
            &projection_key,
            projection_sequence,
        ));
        return true;
    }
    if is_delta_event(kind) {
        let delta = event
            .get("delta")
            .and_then(Value::as_str)
            .unwrap_or_default();
        if delta.is_empty() {
            return false;
        }
        let delta_index = item
            .events
            .iter()
            .position(|candidate| event_type(candidate).as_deref() == Some(kind));
        if let Some(index) = delta_index {
            append_event_delta(&mut item.events[index], delta)
        } else {
            item.events.push(with_projection_metadata(
                event.clone(),
                &projection_key,
                projection_sequence,
            ));
            true
        }
    } else if kind == "tool_interaction" {
        item.events.push(with_projection_metadata(
            event.clone(),
            &projection_key,
            projection_sequence,
        ));
        true
    } else {
        let replacement =
            with_projection_metadata(event.clone(), &projection_key, projection_sequence);
        if let Some(first) = item.events.first_mut() {
            *first = replacement;
        } else {
            item.events.push(replacement);
        }
        true
    }
}

fn append_event_delta(event: &mut Value, delta: &str) -> bool {
    let Some(object) = event.as_object_mut() else {
        return false;
    };
    let existing = object
        .get("delta")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if existing.len() >= MAX_PROJECTED_TEXT_BYTES {
        object.insert("projection_truncated".to_owned(), Value::Bool(true));
        return false;
    }
    let remaining = MAX_PROJECTED_TEXT_BYTES.saturating_sub(existing.len());
    let bounded = truncate_utf8(delta, remaining);
    let mut next = String::with_capacity(existing.len() + bounded.len());
    next.push_str(existing);
    next.push_str(bounded);
    object.insert("delta".to_owned(), Value::String(next));
    if bounded.len() < delta.len() {
        object.insert("projection_truncated".to_owned(), Value::Bool(true));
    }
    true
}

fn truncate_utf8(value: &str, max_bytes: usize) -> &str {
    if value.len() <= max_bytes {
        return value;
    }
    let mut boundary = max_bytes;
    while boundary > 0 && !value.is_char_boundary(boundary) {
        boundary -= 1;
    }
    &value[..boundary]
}

fn initial_recipe(event: &Value, event_type: &str) -> Vec<Value> {
    if event_type == "shell_delta" {
        let mut begin = event.clone();
        begin["type"] = Value::String("shell_begin".to_owned());
        if let Some(object) = begin.as_object_mut() {
            object.remove("delta");
        }
        return vec![begin, event.clone()];
    }
    vec![event.clone()]
}

fn with_projection_metadata(mut event: Value, key: &str, sequence: u64) -> Value {
    if let Some(object) = event.as_object_mut() {
        object.insert("projection_key".to_owned(), Value::String(key.to_owned()));
        object.insert(
            "projection_sequence".to_owned(),
            Value::Number(sequence.into()),
        );
    }
    event
}

fn acknowledge_transcript(state: &mut ConversationTurnProjection, entry: &Value) -> bool {
    let role = entry
        .get("role")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .trim()
        .to_ascii_lowercase();
    if role == "status" {
        let changed = !state.items.is_empty();
        state.items.clear();
        if changed {
            state.revision = state.revision.saturating_add(1);
        }
        return changed;
    }
    let Some(identity) = transcript_identity(state, entry, &role) else {
        return false;
    };
    remember_durable_lineage(state, &identity.lineage);
    let before = state.items.len();
    state.items.retain(|item| item.lineage != identity.lineage);
    if state.items.len() == before {
        return false;
    }
    state.revision = state.revision.saturating_add(1);
    true
}

fn remember_durable_lineage(state: &mut ConversationTurnProjection, lineage: &str) {
    if state
        .durable_lineages
        .iter()
        .any(|candidate| candidate == lineage)
    {
        return;
    }
    if state.durable_lineages.len() >= MAX_DURABLE_LINEAGES {
        state.durable_lineages.pop_front();
    }
    state.durable_lineages.push_back(lineage.to_owned());
}

fn live_identity(
    state: &ConversationTurnProjection,
    event: &Value,
    event_type: &str,
) -> Option<ProjectionIdentity> {
    let family = live_family(event_type)?.to_owned();
    let turn_id =
        clean_field(event, &["turn_id", "turnId"]).or_else(|| state.active_turn_id.clone());
    let subagent_id = clean_field(event, &["subagent_id", "subagentId"]);
    let source_id = clean_field(event, &["id", "item_id", "itemId", "card_id", "cardId"])
        .or_else(|| {
            state
                .items
                .iter()
                .rev()
                .find(|item| {
                    item.family == family
                        && item.turn_id == turn_id
                        && item.subagent_id == subagent_id
                })
                .map(|item| item.source_id.clone())
        })
        .unwrap_or_else(|| format!("{family}:{}", subagent_id.as_deref().unwrap_or("root")));
    let key = projection_key(
        &family,
        turn_id.as_deref(),
        &source_id,
        subagent_id.as_deref(),
    );
    let lineage = projection_lineage(turn_id.as_deref(), &source_id, subagent_id.as_deref());
    Some(ProjectionIdentity {
        key,
        lineage,
        family,
        source_id,
        turn_id,
        subagent_id,
    })
}

fn transcript_identity(
    state: &ConversationTurnProjection,
    entry: &Value,
    role: &str,
) -> Option<ProjectionIdentity> {
    let family = transcript_family(role)?.to_owned();
    let turn_id =
        clean_field(entry, &["turn_id", "turnId"]).or_else(|| state.active_turn_id.clone());
    let subagent_id = clean_field(entry, &["subagent_id", "subagentId"]);
    let source_id =
        clean_field(entry, &["id", "item_id", "itemId", "card_id", "cardId"]).or_else(|| {
            state
                .items
                .iter()
                .rev()
                .find(|item| {
                    item.family == family
                        && item.turn_id == turn_id
                        && item.subagent_id == subagent_id
                })
                .map(|item| item.source_id.clone())
        })?;
    let key = projection_key(
        &family,
        turn_id.as_deref(),
        &source_id,
        subagent_id.as_deref(),
    );
    let lineage = projection_lineage(turn_id.as_deref(), &source_id, subagent_id.as_deref());
    Some(ProjectionIdentity {
        key,
        lineage,
        family,
        source_id,
        turn_id,
        subagent_id,
    })
}

fn projection_key(
    family: &str,
    turn_id: Option<&str>,
    source_id: &str,
    subagent_id: Option<&str>,
) -> String {
    format!(
        "{}:{}:{}:{}",
        turn_id.unwrap_or("pending"),
        family,
        subagent_id.unwrap_or("root"),
        source_id
    )
}

fn projection_lineage(turn_id: Option<&str>, source_id: &str, subagent_id: Option<&str>) -> String {
    format!(
        "{}:{}:{}",
        turn_id.unwrap_or("pending"),
        subagent_id.unwrap_or("root"),
        source_id
    )
}

fn event_type(value: &Value) -> Option<String> {
    value
        .get("type")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_ascii_lowercase)
}

fn clean_field(value: &Value, keys: &[&str]) -> Option<String> {
    let object = value.as_object()?;
    keys.iter()
        .filter_map(|key| object.get(*key).and_then(Value::as_str))
        .map(str::trim)
        .find(|value| !value.is_empty())
        .map(ToOwned::to_owned)
}

fn is_projection_event(event_type: &str) -> bool {
    live_family(event_type).is_some() || event_type == "status"
}

fn live_family(event_type: &str) -> Option<&'static str> {
    match event_type {
        "assistant_delta" | "assistant_end" | "assistant_finalize" => Some("assistant"),
        "reasoning_delta" | "reasoning_end" | "reasoning_finalize" => Some("reasoning"),
        "shell_begin" | "shell_delta" | "shell_end" | "command_result" => Some("command"),
        "tool_begin" | "tool_delta" | "tool_end" | "tool_interaction" => Some("tool"),
        "subagent_start" | "subagent_end" => Some("subagent"),
        "diff" | "diff_declined" => Some("diff"),
        "view" => Some("view"),
        "search" => Some("search"),
        "plan" => Some("plan"),
        "error" | "warning" => Some("notice"),
        "context_compacted" => Some("context_compacted"),
        _ => None,
    }
}

fn transcript_family(role: &str) -> Option<&'static str> {
    match role {
        "assistant" => Some("assistant"),
        "reasoning" => Some("reasoning"),
        "command" => Some("command"),
        "tool" | "mcp_tool" => Some("tool"),
        "subagent_start" | "subagent_end" => Some("subagent"),
        "diff" => Some("diff"),
        "view" => Some("view"),
        "search" | "web_search" => Some("search"),
        "plan" => Some("plan"),
        "error" | "warning" => Some("notice"),
        "context_compacted" => Some("context_compacted"),
        _ => None,
    }
}

fn is_delta_event(event_type: &str) -> bool {
    matches!(
        event_type,
        "assistant_delta" | "reasoning_delta" | "shell_delta" | "tool_delta"
    )
}

fn is_final_event(event_type: &str) -> bool {
    matches!(
        event_type,
        "assistant_end"
            | "assistant_finalize"
            | "reasoning_end"
            | "reasoning_finalize"
            | "shell_end"
            | "tool_end"
            | "subagent_end"
            | "command_result"
            | "diff"
            | "diff_declined"
            | "view"
            | "search"
            | "plan"
            | "error"
            | "warning"
            | "context_compacted"
    )
}

fn terminal_status_event(value: &Value) -> bool {
    let status = clean_field(value, &["turn_status", "status", "stop_reason"])
        .unwrap_or_default()
        .to_ascii_lowercase();
    matches!(
        status.as_str(),
        "success" | "completed" | "failed" | "error" | "interrupted" | "cancelled" | "end_turn"
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn accumulates_deltas_into_one_projection_item() {
        let store = TurnProjectionStore::default();
        store.begin_send("conv").unwrap();
        store
            .reduce_live(
                "conv",
                &json!({"type":"assistant_delta","id":"msg","turn_id":"turn","delta":"hel"}),
            )
            .unwrap();
        store
            .reduce_live(
                "conv",
                &json!({"type":"assistant_delta","id":"msg","turn_id":"turn","delta":"lo"}),
            )
            .unwrap();
        let snapshot = store.snapshot("conv").unwrap();
        assert_eq!(snapshot.items.len(), 1);
        assert_eq!(snapshot.items[0].events[0]["delta"], "hello");
        assert_eq!(snapshot.items[0].card_id, "active:turn:assistant:root:msg");
        assert_eq!(
            snapshot.items[0].events[0]["projection_card_id"],
            snapshot.items[0].card_id
        );
        assert_eq!(snapshot.items[0].scope, "active");
        assert_eq!(
            snapshot.items[0].events[0]["projection_card_scope"],
            "active"
        );
    }

    #[test]
    fn shell_delta_snapshot_replays_begin_then_accumulated_output() {
        let store = TurnProjectionStore::default();
        store.begin_send("conv").unwrap();
        store
            .reduce_live(
                "conv",
                &json!({"type":"shell_delta","id":"shell","turn_id":"turn","command":"printf hi","delta":"h"}),
            )
            .unwrap();
        store
            .reduce_live(
                "conv",
                &json!({"type":"shell_delta","id":"shell","turn_id":"turn","command":"printf hi","delta":"i"}),
            )
            .unwrap();

        let snapshot = store.snapshot("conv").unwrap();
        let events = &snapshot.items[0].events;
        assert_eq!(events[0]["type"], "shell_begin");
        assert_eq!(events[1]["type"], "shell_delta");
        assert_eq!(events[1]["delta"], "hi");
        assert_eq!(events[0]["projection_key"], events[1]["projection_key"]);
    }

    #[test]
    fn projection_read_pairs_durable_window_with_live_snapshot() {
        let store = TurnProjectionStore::default();
        let conversations = ConversationStore::new(temp_root("paired-read"));
        conversations
            .append_transcript("conv", json!({"role":"user","text":"hello"}))
            .unwrap();
        store.begin_send("conv").unwrap();
        store
            .reduce_live(
                "conv",
                &json!({"type":"assistant_delta","id":"msg","turn_id":"turn","delta":"working"}),
            )
            .unwrap();

        let (projection, live) = store
            .project_transcript_with_live(
                &conversations,
                "client",
                "conv",
                TranscriptProjectionAction::Tail,
                100,
                25,
                1024 * 1024,
            )
            .unwrap();
        assert_eq!(projection.cards.len(), 1);
        assert_eq!(projection.cards[0].events[0]["role"], "user");
        assert_eq!(live.items.len(), 1);
        assert_eq!(live.items[0].events[0]["delta"], "working");
    }

    #[test]
    fn transcript_first_prevents_late_final_from_reappearing() {
        let store = TurnProjectionStore::default();
        let conversations = ConversationStore::new(temp_root("transcript-first"));
        store.begin_send("conv").unwrap();
        store
            .reduce_live(
                "conv",
                &json!({"type":"assistant_delta","id":"msg","turn_id":"turn","delta":"hello"}),
            )
            .unwrap();
        store
            .append_transcript_and_acknowledge(
                &conversations,
                "conv",
                json!({"role":"assistant","id":"msg","item_id":"msg","turn_id":"turn","text":"hello"}),
            )
            .unwrap();
        assert!(store.snapshot("conv").unwrap().items.is_empty());
        assert!(store
            .reduce_live(
                "conv",
                &json!({"type":"assistant_finalize","id":"msg","turn_id":"turn","text":"hello"}),
            )
            .unwrap()
            .is_none());
        assert!(store.snapshot("conv").unwrap().items.is_empty());
    }

    #[test]
    fn live_first_retires_when_transcript_arrives() {
        let store = TurnProjectionStore::default();
        let conversations = ConversationStore::new(temp_root("live-first"));
        store.begin_send("conv").unwrap();
        store
            .reduce_live(
                "conv",
                &json!({"type":"tool_end","id":"tool","turn_id":"turn","tool":"read","result":{}}),
            )
            .unwrap();
        assert_eq!(store.snapshot("conv").unwrap().items.len(), 1);
        store
            .append_transcript_and_acknowledge(
                &conversations,
                "conv",
                json!({"role":"tool","id":"tool","item_id":"tool","turn_id":"turn","tool":"read","result":{}}),
            )
            .unwrap();
        assert!(store.snapshot("conv").unwrap().items.is_empty());
    }

    #[test]
    fn specialized_command_replaces_generic_tool_alias() {
        let store = TurnProjectionStore::default();
        store.begin_send("conv").unwrap();
        store
            .reduce_live(
                "conv",
                &json!({"type":"tool_begin","id":"exec","turn_id":"turn","tool":"shell"}),
            )
            .unwrap();
        let initial = store.snapshot("conv").unwrap();
        assert_eq!(initial.items.len(), 1);
        assert_eq!(initial.items[0].family, "tool");

        store
            .reduce_live(
                "conv",
                &json!({"type":"shell_begin","id":"exec","turn_id":"turn","command":"printf hi"}),
            )
            .unwrap();
        let specialized = store.snapshot("conv").unwrap();
        assert_eq!(specialized.items.len(), 1);
        assert_eq!(specialized.items[0].family, "command");
        assert_eq!(specialized.items[0].sequence, initial.items[0].sequence);

        assert!(
            store
                .reduce_live(
                    "conv",
                    &json!({"type":"tool_end","id":"exec","turn_id":"turn","tool":"shell"}),
                )
                .unwrap()
                .is_none()
        );
        assert_eq!(store.snapshot("conv").unwrap().items[0].family, "command");
    }

    #[test]
    fn durable_command_retires_and_tombstones_generic_tool_alias() {
        let store = TurnProjectionStore::default();
        let conversations = ConversationStore::new(temp_root("cross-family-handoff"));
        store.begin_send("conv").unwrap();
        store
            .reduce_live(
                "conv",
                &json!({"type":"tool_begin","id":"exec","turn_id":"turn","tool":"shell"}),
            )
            .unwrap();
        store
            .append_transcript_and_acknowledge(
                &conversations,
                "conv",
                json!({"role":"command","id":"exec","item_id":"exec","turn_id":"turn","command":"printf hi"}),
            )
            .unwrap();
        assert!(store.snapshot("conv").unwrap().items.is_empty());

        assert!(
            store
                .reduce_live(
                    "conv",
                    &json!({"type":"tool_end","id":"exec","turn_id":"turn","tool":"shell"}),
                )
                .unwrap()
                .is_none()
        );
        assert!(store.snapshot("conv").unwrap().items.is_empty());
    }

    #[test]
    fn durable_turn_status_clears_orphaned_items() {
        let store = TurnProjectionStore::default();
        let conversations = ConversationStore::new(temp_root("turn-status"));
        store.begin_send("conv").unwrap();
        store
            .reduce_live(
                "conv",
                &json!({"type":"shell_begin","id":"shell","turn_id":"turn","command":"sleep"}),
            )
            .unwrap();
        store
            .append_transcript_and_acknowledge(
                &conversations,
                "conv",
                json!({"role":"status","turn_id":"turn","status":"interrupted"}),
            )
            .unwrap();
        assert!(store.snapshot("conv").unwrap().items.is_empty());
    }

    fn temp_root(label: &str) -> std::path::PathBuf {
        static NEXT_ID: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
        std::env::temp_dir().join(format!(
            "als-rs-turn-projection-{label}-{}-{}",
            std::process::id(),
            NEXT_ID.fetch_add(1, std::sync::atomic::Ordering::Relaxed),
        ))
    }
}
