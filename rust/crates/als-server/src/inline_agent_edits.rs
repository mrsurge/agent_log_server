use als_adapter_protocol::JsonMap;
use anyhow::{Result, anyhow, bail};
use serde_json::{Map, Value, json};
use std::{
    collections::BTreeMap,
    sync::{Arc, Mutex},
};

#[derive(Clone, Default)]
pub struct InlineAgentEditLedger {
    inner: Arc<Mutex<InlineAgentEditState>>,
}

#[derive(Clone, Debug, Default)]
struct InlineAgentEditState {
    ledger_revision: u64,
    records: Vec<InlineAgentEditRecord>,
}

#[derive(Clone, Debug)]
struct InlineAgentEditRecord {
    source: InlineEditSource,
    uri: String,
    edit_id: String,
    revision: u64,
    payload: JsonMap,
}

#[derive(Clone, Debug, Default, Eq, PartialEq, Ord, PartialOrd)]
struct InlineEditSource {
    conversation_id: Option<String>,
    session_id: Option<String>,
    thread_id: Option<String>,
    project_path: Option<String>,
}

impl InlineAgentEditLedger {
    pub fn publish(&self, params: &JsonMap) -> Result<Value> {
        let source = InlineEditSource::from_params(params);
        let replace = bool_field(params, "replace").unwrap_or(false);
        let edits = params
            .get("edits")
            .and_then(Value::as_array)
            .ok_or_else(|| anyhow!("edits is required"))?;

        let mut guard = self
            .inner
            .lock()
            .map_err(|_| anyhow!("inline agent edit ledger lock poisoned"))?;

        guard.ledger_revision = guard.ledger_revision.saturating_add(1);
        let ledger_revision = guard.ledger_revision;
        if replace {
            guard.records.retain(|record| record.source != source);
        }

        let mut accepted_count = 0usize;
        let mut visible_count = 0usize;
        let mut affected_uris: Vec<String> = Vec::new();
        for edit in edits {
            let edit_object = edit
                .as_object()
                .ok_or_else(|| anyhow!("each edit must be an object"))?;
            let uri = string_from_map(edit_object, &["uri"])
                .or_else(|| string_from_params(params, &["uri"]))
                .ok_or_else(|| anyhow!("edit.uri is required"))?;
            let edit_id = string_from_map(edit_object, &["editId", "edit_id", "id"])
                .ok_or_else(|| anyhow!("edit.editId is required"))?;
            let mut payload = edit_object.clone();
            insert_source_fields(&mut payload, &source);
            payload.insert("uri".to_owned(), Value::String(uri.clone()));
            payload.insert("editId".to_owned(), Value::String(edit_id.clone()));
            payload
                .entry("state".to_owned())
                .or_insert_with(|| Value::String("pending".to_owned()));
            normalize_hunk_payloads(&mut payload);
            let revision = numeric_field(&payload, "revision").unwrap_or(ledger_revision);
            payload.insert("revision".to_owned(), Value::Number(revision.into()));
            let record = InlineAgentEditRecord {
                source: source.clone(),
                uri: uri.clone(),
                edit_id: edit_id.clone(),
                revision,
                payload,
            };
            if state_from_record(&record) == "accepted" {
                accepted_count += 1;
            } else {
                visible_count += 1;
            }
            if !affected_uris.iter().any(|existing| existing == &uri) {
                affected_uris.push(uri.clone());
            }
            if let Some(existing) = guard
                .records
                .iter_mut()
                .find(|record| record.source == source && record.edit_id == edit_id)
            {
                *existing = record;
            } else {
                guard.records.push(record);
            }
        }

        Ok(json!({
            "ok": true,
            "ledgerRevision": ledger_revision,
            "conversationId": source.conversation_id,
            "sessionId": source.session_id,
            "threadId": source.thread_id,
            "projectPath": source.project_path,
            "acceptedCount": accepted_count,
            "visibleCount": visible_count,
            "affectedUris": affected_uris,
            "transport": "rpc",
        }))
    }

    pub fn document_state(&self, params: &JsonMap) -> Result<Value> {
        let uri = string_from_params(params, &["uri"]).ok_or_else(|| anyhow!("uri is required"))?;
        let known_revision = numeric_field(params, "knownLedgerRevision")
            .or_else(|| numeric_field(params, "known_ledger_revision"));
        let guard = self
            .inner
            .lock()
            .map_err(|_| anyhow!("inline agent edit ledger lock poisoned"))?;
        if known_revision.is_some_and(|known| known >= guard.ledger_revision) {
            return Ok(json!({
                "ok": true,
                "uri": uri,
                "projectPath": string_from_params(params, &["projectPath", "project_path"]),
                "ledgerRevision": guard.ledger_revision,
                "notModified": true,
                "sources": [],
                "edits": [],
                "transport": "rpc",
            }));
        }
        Ok(document_state_from_records(&guard, &uri, params))
    }

    pub fn list(&self, params: &JsonMap) -> Result<Value> {
        let guard = self
            .inner
            .lock()
            .map_err(|_| anyhow!("inline agent edit ledger lock poisoned"))?;
        let records = filter_records(&guard.records, params)
            .into_iter()
            .map(|record| Value::Object(record.payload.clone()))
            .collect::<Vec<_>>();
        let count = records.len();
        Ok(json!({
            "ok": true,
            "ledgerRevision": guard.ledger_revision,
            "edits": records,
            "count": count,
            "transport": "rpc",
        }))
    }

    pub fn clear(&self, params: &JsonMap) -> Result<Value> {
        let mut guard = self
            .inner
            .lock()
            .map_err(|_| anyhow!("inline agent edit ledger lock poisoned"))?;
        let before = guard.records.len();
        let uris = string_array_field(params, "uris");
        guard.records.retain(|record| {
            if !record_matches_filter(record, params) {
                return true;
            }
            if !uris.is_empty() && !uris.iter().any(|uri| uri == &record.uri) {
                return true;
            }
            false
        });
        let removed_count = before.saturating_sub(guard.records.len());
        if removed_count > 0 {
            guard.ledger_revision = guard.ledger_revision.saturating_add(1);
        }
        Ok(json!({
            "ok": true,
            "ledgerRevision": guard.ledger_revision,
            "removedCount": removed_count,
            "transport": "rpc",
        }))
    }

    pub fn decide(&self, params: &JsonMap) -> Result<Value> {
        let decision = string_from_params(params, &["decision"])
            .ok_or_else(|| anyhow!("decision is required"))?;
        let target_state = match decision.as_str() {
            "accept" | "accepted" => "accepted",
            "reject" | "rejected" => "rejected",
            _ => bail!("decision must be accept or reject"),
        };
        let uri = string_from_params(params, &["uri"]);
        let edit_id = string_from_params(params, &["editId", "edit_id", "id"])
            .ok_or_else(|| anyhow!("editId is required"))?;
        let hunk_id = string_from_params(params, &["hunkId", "hunk_id"]);
        let known_revision = numeric_field(params, "knownRevision")
            .or_else(|| numeric_field(params, "known_revision"));

        let mut guard = self
            .inner
            .lock()
            .map_err(|_| anyhow!("inline agent edit ledger lock poisoned"))?;
        let Some(index) = guard.records.iter().position(|record| {
            record.edit_id == edit_id
                && uri.as_ref().map_or(true, |uri| &record.uri == uri)
                && record_matches_source_filter(record, params)
        }) else {
            bail!("edit not found");
        };

        guard.ledger_revision = guard.ledger_revision.saturating_add(1);
        let ledger_revision = guard.ledger_revision;
        let record = &mut guard.records[index];
        let stale = known_revision.is_some_and(|known| known != record.revision);
        let next_state = if stale { "stale" } else { target_state };
        record.revision = ledger_revision;
        record
            .payload
            .insert("revision".to_owned(), Value::Number(ledger_revision.into()));
        record
            .payload
            .insert("state".to_owned(), Value::String(next_state.to_owned()));
        if stale {
            record.payload.insert(
                "message".to_owned(),
                Value::String("knownRevision does not match current edit revision".to_owned()),
            );
        } else {
            record.payload.remove("message");
        }
        if let Some(hunk_id) = hunk_id.as_deref() {
            update_hunk_state(&mut record.payload, hunk_id, next_state, stale);
        }
        let uri = record.uri.clone();
        Ok(document_state_from_records(&guard, &uri, params))
    }
}

fn document_state_from_records(state: &InlineAgentEditState, uri: &str, params: &JsonMap) -> Value {
    let filtered = state
        .records
        .iter()
        .filter(|record| record.uri == uri)
        .filter(|record| record_matches_filter(record, params))
        .collect::<Vec<_>>();
    let mut grouped: BTreeMap<InlineEditSource, Vec<Value>> = BTreeMap::new();
    for record in &filtered {
        grouped
            .entry(record.source.clone())
            .or_default()
            .push(Value::Object(record.payload.clone()));
    }
    let sources = grouped
        .into_iter()
        .map(|(source, edits)| {
            json!({
                "conversationId": source.conversation_id,
                "sessionId": source.session_id,
                "threadId": source.thread_id,
                "projectPath": source.project_path,
                "edits": edits,
            })
        })
        .collect::<Vec<_>>();
    let edits = filtered
        .into_iter()
        .map(|record| Value::Object(record.payload.clone()))
        .collect::<Vec<_>>();
    json!({
        "ok": true,
        "uri": uri,
        "projectPath": string_from_params(params, &["projectPath", "project_path"]),
        "ledgerRevision": state.ledger_revision,
        "notModified": false,
        "sources": sources,
        "edits": edits,
        "transport": "rpc",
    })
}

fn filter_records<'a>(
    records: &'a [InlineAgentEditRecord],
    params: &JsonMap,
) -> Vec<&'a InlineAgentEditRecord> {
    records
        .iter()
        .filter(|record| record_matches_filter(record, params))
        .collect()
}

fn record_matches_filter(record: &InlineAgentEditRecord, params: &JsonMap) -> bool {
    if string_from_params(params, &["uri"]).is_some_and(|uri| uri != record.uri) {
        return false;
    }
    if string_from_params(params, &["editId", "edit_id", "diffId", "diff_id", "id"])
        .is_some_and(|edit_id| edit_id != record.edit_id)
    {
        return false;
    }
    record_matches_source_filter(record, params)
}

fn record_matches_source_filter(record: &InlineAgentEditRecord, params: &JsonMap) -> bool {
    source_field_matches(
        record.source.conversation_id.as_deref(),
        params,
        &["conversationId", "conversation_id"],
    ) && source_field_matches(
        record.source.session_id.as_deref(),
        params,
        &["sessionId", "session_id"],
    ) && source_field_matches(
        record.source.thread_id.as_deref(),
        params,
        &["threadId", "thread_id"],
    ) && source_field_matches(
        record.source.project_path.as_deref(),
        params,
        &["projectPath", "project_path"],
    )
}

fn source_field_matches(value: Option<&str>, params: &JsonMap, keys: &[&str]) -> bool {
    string_from_params(params, keys).map_or(true, |expected| value == Some(expected.as_str()))
}

fn insert_source_fields(payload: &mut JsonMap, source: &InlineEditSource) {
    if let Some(value) = source.conversation_id.as_ref() {
        payload.insert("conversationId".to_owned(), Value::String(value.clone()));
    }
    if let Some(value) = source.session_id.as_ref() {
        payload.insert("sessionId".to_owned(), Value::String(value.clone()));
    }
    if let Some(value) = source.thread_id.as_ref() {
        payload.insert("threadId".to_owned(), Value::String(value.clone()));
    }
    if let Some(value) = source.project_path.as_ref() {
        payload.insert("projectPath".to_owned(), Value::String(value.clone()));
    }
}

fn normalize_hunk_payloads(payload: &mut JsonMap) {
    let Some(hunks) = payload.get_mut("hunks").and_then(Value::as_array_mut) else {
        return;
    };
    for hunk in hunks {
        let Some(hunk) = hunk.as_object_mut() else {
            continue;
        };
        if let Some(value) = string_from_map(hunk, &["hunkId", "hunk_id", "id"]) {
            hunk.insert("hunkId".to_owned(), Value::String(value));
        }
        hunk.entry("state".to_owned())
            .or_insert_with(|| Value::String("pending".to_owned()));
    }
}

fn update_hunk_state(payload: &mut JsonMap, hunk_id: &str, state: &str, stale: bool) {
    let Some(hunks) = payload.get_mut("hunks").and_then(Value::as_array_mut) else {
        return;
    };
    for hunk in hunks {
        let Some(hunk_object) = hunk.as_object_mut() else {
            continue;
        };
        let Some(candidate) = string_from_map(hunk_object, &["hunkId", "hunk_id", "id"]) else {
            continue;
        };
        if candidate != hunk_id {
            continue;
        }
        hunk_object.insert("state".to_owned(), Value::String(state.to_owned()));
        if stale {
            hunk_object.insert(
                "message".to_owned(),
                Value::String("knownRevision does not match current edit revision".to_owned()),
            );
        } else {
            hunk_object.remove("message");
        }
    }
}

fn state_from_record(record: &InlineAgentEditRecord) -> String {
    record
        .payload
        .get("state")
        .and_then(Value::as_str)
        .unwrap_or("pending")
        .to_owned()
}

impl InlineEditSource {
    fn from_params(params: &JsonMap) -> Self {
        Self {
            conversation_id: string_from_params(params, &["conversationId", "conversation_id"]),
            session_id: string_from_params(params, &["sessionId", "session_id"]),
            thread_id: string_from_params(params, &["threadId", "thread_id"]),
            project_path: string_from_params(params, &["projectPath", "project_path"]),
        }
    }
}

fn string_from_params(params: &JsonMap, keys: &[&str]) -> Option<String> {
    keys.iter().find_map(|key| {
        params
            .get(*key)
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .map(ToOwned::to_owned)
    })
}

fn string_from_map(params: &Map<String, Value>, keys: &[&str]) -> Option<String> {
    keys.iter().find_map(|key| {
        params
            .get(*key)
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .map(ToOwned::to_owned)
    })
}

fn numeric_field(params: &Map<String, Value>, key: &str) -> Option<u64> {
    params.get(key).and_then(Value::as_u64)
}

fn bool_field(params: &Map<String, Value>, key: &str) -> Option<bool> {
    params.get(key).and_then(Value::as_bool)
}

fn string_array_field(params: &JsonMap, key: &str) -> Vec<String> {
    params
        .get(key)
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(Value::as_str)
                .map(str::trim)
                .filter(|value| !value.is_empty())
                .map(ToOwned::to_owned)
                .collect()
        })
        .unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn object(value: Value) -> JsonMap {
        value.as_object().cloned().unwrap()
    }

    #[test]
    fn document_state_groups_sources_by_uri() {
        let ledger = InlineAgentEditLedger::default();
        let result = ledger
            .publish(&object(json!({
                "conversationId": "conv-a",
                "sessionId": "session-a",
                "projectPath": "/repo",
                "replace": true,
                "edits": [{
                    "editId": "edit-1",
                    "uri": "file:///repo/src/lib.rs",
                    "rel": "src/lib.rs",
                    "hunks": [{"hunkId": "hunk-1", "kind": "modified"}]
                }]
            })))
            .unwrap();
        assert_eq!(result["ok"], true);

        let state = ledger
            .document_state(&object(json!({
                "uri": "file:///repo/src/lib.rs",
                "projectPath": "/repo"
            })))
            .unwrap();
        assert_eq!(state["notModified"], false);
        assert_eq!(state["sources"].as_array().unwrap().len(), 1);
        assert_eq!(state["edits"].as_array().unwrap().len(), 1);
        assert_eq!(state["edits"][0]["state"], "pending");
        assert_eq!(state["edits"][0]["hunks"][0]["state"], "pending");
    }

    #[test]
    fn known_ledger_revision_returns_not_modified() {
        let ledger = InlineAgentEditLedger::default();
        let result = ledger
            .publish(&object(json!({
                "conversationId": "conv-a",
                "edits": [{
                    "editId": "edit-1",
                    "uri": "file:///repo/src/lib.rs"
                }]
            })))
            .unwrap();
        let revision = result["ledgerRevision"].as_u64().unwrap();
        let state = ledger
            .document_state(&object(json!({
                "uri": "file:///repo/src/lib.rs",
                "knownLedgerRevision": revision
            })))
            .unwrap();
        assert_eq!(state["notModified"], true);
        assert_eq!(state["sources"].as_array().unwrap().len(), 0);
    }

    #[test]
    fn decision_updates_edit_and_hunk_state() {
        let ledger = InlineAgentEditLedger::default();
        ledger
            .publish(&object(json!({
                "conversationId": "conv-a",
                "edits": [{
                    "editId": "edit-1",
                    "uri": "file:///repo/src/lib.rs",
                    "hunks": [{"hunkId": "hunk-1"}]
                }]
            })))
            .unwrap();
        let state = ledger
            .decide(&object(json!({
                "decision": "accept",
                "conversationId": "conv-a",
                "uri": "file:///repo/src/lib.rs",
                "editId": "edit-1",
                "hunkId": "hunk-1",
                "knownRevision": 1
            })))
            .unwrap();
        assert_eq!(state["edits"][0]["state"], "accepted");
        assert_eq!(state["edits"][0]["hunks"][0]["state"], "accepted");
        assert_eq!(state["ledgerRevision"], 2);
    }

    #[test]
    fn stale_decision_marks_stale_without_applying_choice() {
        let ledger = InlineAgentEditLedger::default();
        ledger
            .publish(&object(json!({
                "conversationId": "conv-a",
                "edits": [{
                    "editId": "edit-1",
                    "uri": "file:///repo/src/lib.rs",
                    "hunks": [{"hunkId": "hunk-1"}]
                }]
            })))
            .unwrap();
        let state = ledger
            .decide(&object(json!({
                "decision": "reject",
                "conversationId": "conv-a",
                "uri": "file:///repo/src/lib.rs",
                "editId": "edit-1",
                "hunkId": "hunk-1",
                "knownRevision": 99
            })))
            .unwrap();
        assert_eq!(state["edits"][0]["state"], "stale");
        assert_eq!(state["edits"][0]["hunks"][0]["state"], "stale");
        assert!(
            state["edits"][0]["message"]
                .as_str()
                .unwrap()
                .contains("knownRevision")
        );
    }

    #[test]
    fn clear_removes_matching_uri() {
        let ledger = InlineAgentEditLedger::default();
        ledger
            .publish(&object(json!({
                "conversationId": "conv-a",
                "edits": [
                    {"editId": "edit-1", "uri": "file:///repo/src/lib.rs"},
                    {"editId": "edit-2", "uri": "file:///repo/src/main.rs"}
                ]
            })))
            .unwrap();
        let result = ledger
            .clear(&object(json!({
                "conversationId": "conv-a",
                "uris": ["file:///repo/src/lib.rs"]
            })))
            .unwrap();
        assert_eq!(result["removedCount"], 1);
        let state = ledger
            .document_state(&object(json!({"uri": "file:///repo/src/lib.rs"})))
            .unwrap();
        assert_eq!(state["edits"].as_array().unwrap().len(), 0);
        let remaining = ledger
            .document_state(&object(json!({"uri": "file:///repo/src/main.rs"})))
            .unwrap();
        assert_eq!(remaining["edits"].as_array().unwrap().len(), 1);
    }

    #[test]
    fn clear_removes_matching_edit_id_only() {
        let ledger = InlineAgentEditLedger::default();
        ledger
            .publish(&object(json!({
                "conversationId": "conv-a",
                "edits": [
                    {"editId": "edit-1", "uri": "file:///repo/src/lib.rs"},
                    {"editId": "edit-2", "uri": "file:///repo/src/lib.rs"}
                ]
            })))
            .unwrap();
        let result = ledger
            .clear(&object(json!({
                "conversationId": "conv-a",
                "editId": "edit-1"
            })))
            .unwrap();
        assert_eq!(result["removedCount"], 1);
        let state = ledger
            .document_state(&object(json!({"uri": "file:///repo/src/lib.rs"})))
            .unwrap();
        let edits = state["edits"].as_array().unwrap();
        assert_eq!(edits.len(), 1);
        assert_eq!(edits[0]["editId"], "edit-2");
    }
}
