use serde::Serialize;
use serde_json::{Value, json};
use std::collections::{HashMap, HashSet};

#[derive(Clone, Debug, Default)]
pub struct TranscriptCardIndex {
    cards: Vec<IndexedTranscriptCard>,
    cards_by_id: HashMap<String, usize>,
    line_projections: HashMap<usize, IndexedLineProjection>,
    runtime_state_lines: HashMap<String, usize>,
}

#[derive(Clone, Debug)]
struct IndexedLineProjection {
    card_index: usize,
    operation: &'static str,
}

#[derive(Clone, Debug)]
pub struct TranscriptCardEventMetadata {
    pub card_id: String,
    pub card_index: usize,
    pub version: u64,
    pub operation: &'static str,
    pub parent_card_id: Option<String>,
}

#[derive(Clone, Debug)]
struct IndexedTranscriptCard {
    card_id: String,
    family: String,
    parent_card_id: Option<String>,
    version: u64,
    events: Vec<IndexedCardEvent>,
}

#[derive(Clone, Debug)]
struct IndexedCardEvent {
    source: IndexedCardEventSource,
    operation: &'static str,
}

#[derive(Clone, Debug)]
enum IndexedCardEventSource {
    Line(usize),
    Synthetic(Value),
}

#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct TranscriptCardRecipe {
    pub card_id: String,
    pub card_index: usize,
    pub version: u64,
    pub family: String,
    pub scope: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub parent_card_id: Option<String>,
    pub events: Vec<Value>,
}

#[derive(Clone, Debug)]
pub struct TranscriptCardSelection {
    pub card_indices: Vec<usize>,
    pub runtime_state_lines: Vec<usize>,
}

#[derive(Clone, Debug)]
pub enum IndexedProjectionEvent {
    Line {
        line_index: usize,
        operation: &'static str,
    },
    Synthetic {
        value: Value,
        operation: &'static str,
    },
}

#[derive(Clone, Debug)]
pub struct IndexedCardRecipe {
    pub card_id: String,
    pub card_index: usize,
    pub version: u64,
    pub family: String,
    pub parent_card_id: Option<String>,
    pub events: Vec<IndexedProjectionEvent>,
}

impl TranscriptCardIndex {
    pub fn total_cards(&self) -> usize {
        self.cards.len()
    }

    pub fn apply_record(&mut self, line_index: usize, record: &Value) {
        let Some(role) = clean_field(record, &["role"]).map(|value| value.to_ascii_lowercase())
        else {
            return;
        };
        if is_internal(record) {
            return;
        }
        if is_runtime_state_role(&role) {
            self.runtime_state_lines.insert(role, line_index);
            return;
        }

        if role == "subagent_end" {
            let Some(subagent_id) = clean_field(record, &["id", "subagent_id", "subagentId"])
            else {
                return;
            };
            let card_id = subagent_card_id(&subagent_id);
            self.append_known_update(&card_id, line_index);
            return;
        }

        if role == "agent_pty" {
            self.apply_agent_pty_record(line_index, record);
            return;
        }

        if role == "subagent_start" {
            let Some(subagent_id) = clean_field(record, &["id", "subagent_id", "subagentId"])
            else {
                return;
            };
            let card_id = subagent_card_id(&subagent_id);
            if let Some(index) = self.cards_by_id.get(&card_id).copied() {
                self.cards[index].version = self.cards[index].version.saturating_add(1);
                self.cards[index].events.push(IndexedCardEvent {
                    source: IndexedCardEventSource::Line(line_index),
                    operation: "update",
                });
                self.line_projections.insert(
                    line_index,
                    IndexedLineProjection {
                        card_index: index,
                        operation: "update",
                    },
                );
            } else {
                self.create_card(
                    card_id,
                    "subagent".to_owned(),
                    None,
                    IndexedCardEventSource::Line(line_index),
                );
            }
            return;
        }

        let parent_card_id = clean_field(record, &["subagent_id", "subagentId"])
            .map(|subagent_id| self.ensure_subagent_parent(&subagent_id, line_index));
        let family = normalized_family(&role).to_owned();
        let explicit_card_id = clean_field(record, &["card_id", "cardId"]);
        if let Some(explicit_card_id) = explicit_card_id {
            self.upsert_snapshot_card(explicit_card_id, family, parent_card_id, line_index);
            return;
        }

        let Some(source_id) = clean_field(record, &["id", "item_id", "itemId"]) else {
            self.create_card(
                format!("{family}:line-{line_index}:{line_index}"),
                family,
                parent_card_id,
                IndexedCardEventSource::Line(line_index),
            );
            return;
        };
        self.upsert_snapshot_card(
            format!("{family}:{source_id}"),
            family,
            parent_card_id,
            line_index,
        );
    }

    fn upsert_snapshot_card(
        &mut self,
        card_id: String,
        family: String,
        parent_card_id: Option<String>,
        line_index: usize,
    ) {
        if let Some(index) = self.cards_by_id.get(&card_id).copied() {
            let card = &mut self.cards[index];
            card.family = family;
            card.parent_card_id = parent_card_id.or_else(|| card.parent_card_id.clone());
            card.version = card.version.saturating_add(1);
            card.events.clear();
            card.events.push(IndexedCardEvent {
                source: IndexedCardEventSource::Line(line_index),
                operation: "create",
            });
            self.line_projections.insert(
                line_index,
                IndexedLineProjection {
                    card_index: index,
                    operation: "update",
                },
            );
            return;
        }
        self.create_card(
            card_id,
            family,
            parent_card_id,
            IndexedCardEventSource::Line(line_index),
        );
    }

    pub fn select(&self, start_card: usize, window_cards: usize) -> TranscriptCardSelection {
        let start = start_card.min(self.cards.len());
        let end = start.saturating_add(window_cards).min(self.cards.len());
        let mut selected = (start..end).collect::<HashSet<_>>();
        let mut required_parents = HashSet::new();

        loop {
            let mut added = false;
            for index in selected.iter().copied().collect::<Vec<_>>() {
                let Some(parent_card_id) = self.cards[index].parent_card_id.as_deref() else {
                    continue;
                };
                let Some(parent_index) = self.cards_by_id.get(parent_card_id).copied() else {
                    continue;
                };
                required_parents.insert(parent_index);
                if selected.insert(parent_index) {
                    added = true;
                }
            }
            if !added {
                break;
            }
        }

        while selected.len() > window_cards {
            let removable = selected
                .iter()
                .copied()
                .filter(|index| !required_parents.contains(index))
                .min();
            let Some(removable) = removable else {
                break;
            };
            selected.remove(&removable);
        }

        let mut card_indices = selected.into_iter().collect::<Vec<_>>();
        card_indices.sort_unstable();
        let mut runtime_state_lines = self
            .runtime_state_lines
            .values()
            .copied()
            .collect::<Vec<_>>();
        runtime_state_lines.sort_unstable();
        TranscriptCardSelection {
            card_indices,
            runtime_state_lines,
        }
    }

    pub fn indexed_recipe(&self, card_index: usize) -> Option<IndexedCardRecipe> {
        let card = self.cards.get(card_index)?;
        Some(IndexedCardRecipe {
            card_id: card.card_id.clone(),
            card_index,
            version: card.version,
            family: card.family.clone(),
            parent_card_id: card.parent_card_id.clone(),
            events: card
                .events
                .iter()
                .map(|event| match &event.source {
                    IndexedCardEventSource::Line(line_index) => IndexedProjectionEvent::Line {
                        line_index: *line_index,
                        operation: event.operation,
                    },
                    IndexedCardEventSource::Synthetic(value) => IndexedProjectionEvent::Synthetic {
                        value: value.clone(),
                        operation: event.operation,
                    },
                })
                .collect(),
        })
    }

    pub fn event_metadata(&self, line_index: usize) -> Option<TranscriptCardEventMetadata> {
        let projection = self.line_projections.get(&line_index)?;
        let card = self.cards.get(projection.card_index)?;
        Some(TranscriptCardEventMetadata {
            card_id: card.card_id.clone(),
            card_index: projection.card_index,
            version: card.version,
            operation: projection.operation,
            parent_card_id: card.parent_card_id.clone(),
        })
    }

    fn apply_agent_pty_record(&mut self, line_index: usize, record: &Value) {
        let event = clean_field(record, &["event", "type"])
            .unwrap_or_default()
            .to_ascii_lowercase();
        let block_id = clean_field(record, &["block_id", "blockId"])
            .or_else(|| nested_clean_field(record, "block", &["block_id", "blockId"]))
            .unwrap_or_else(|| format!("line-{line_index}"));
        let card_id = format!("agent_pty:{block_id}");
        if let Some(index) = self.cards_by_id.get(&card_id).copied() {
            self.cards[index].version = self.cards[index].version.saturating_add(1);
            self.cards[index].events.push(IndexedCardEvent {
                source: IndexedCardEventSource::Line(line_index),
                operation: "update",
            });
            self.line_projections.insert(
                line_index,
                IndexedLineProjection {
                    card_index: index,
                    operation: "update",
                },
            );
            return;
        }
        if event == "agent_block_end" {
            return;
        }
        let parent_card_id = clean_field(record, &["subagent_id", "subagentId"])
            .map(|subagent_id| self.ensure_subagent_parent(&subagent_id, line_index));
        self.create_card(
            card_id,
            "agent_pty".to_owned(),
            parent_card_id,
            IndexedCardEventSource::Line(line_index),
        );
    }

    fn append_known_update(&mut self, card_id: &str, line_index: usize) {
        let Some(index) = self.cards_by_id.get(card_id).copied() else {
            return;
        };
        self.cards[index].version = self.cards[index].version.saturating_add(1);
        self.cards[index].events.push(IndexedCardEvent {
            source: IndexedCardEventSource::Line(line_index),
            operation: "update",
        });
        self.line_projections.insert(
            line_index,
            IndexedLineProjection {
                card_index: index,
                operation: "update",
            },
        );
    }

    fn ensure_subagent_parent(&mut self, subagent_id: &str, line_index: usize) -> String {
        let card_id = subagent_card_id(subagent_id);
        if self.cards_by_id.contains_key(&card_id) {
            return card_id;
        }
        self.create_card(
            card_id.clone(),
            "subagent".to_owned(),
            None,
            IndexedCardEventSource::Synthetic(json!({
                "role": "subagent_start",
                "id": subagent_id,
                "name": "subagent",
                "intent": "earlier in transcript",
                "projection_synthetic": true,
                "order_id": line_index,
            })),
        );
        card_id
    }

    fn create_card(
        &mut self,
        card_id: String,
        family: String,
        parent_card_id: Option<String>,
        source: IndexedCardEventSource,
    ) {
        let index = self.cards.len();
        if let IndexedCardEventSource::Line(line_index) = &source {
            self.line_projections.insert(
                *line_index,
                IndexedLineProjection {
                    card_index: index,
                    operation: "create",
                },
            );
        }
        self.cards_by_id.insert(card_id.clone(), index);
        self.cards.push(IndexedTranscriptCard {
            card_id,
            family,
            parent_card_id,
            version: 1,
            events: vec![IndexedCardEvent {
                source,
                operation: "create",
            }],
        });
    }
}

pub fn apply_projection_metadata(
    mut event: Value,
    card_id: &str,
    card_index: usize,
    card_version: u64,
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
            Value::Number((card_index as u64).into()),
        );
        object.insert(
            "projection_card_version".to_owned(),
            Value::Number(card_version.into()),
        );
        object.insert(
            "projection_card_op".to_owned(),
            Value::String(operation.to_owned()),
        );
        object.insert(
            "projection_card_scope".to_owned(),
            Value::String("durable".to_owned()),
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

fn normalized_family(role: &str) -> &str {
    match role {
        "mcp_tool" => "tool",
        "subagent_start" | "subagent_end" => "subagent",
        other => other,
    }
}

fn is_runtime_state_role(role: &str) -> bool {
    matches!(role, "mode" | "status" | "token_usage")
}

fn is_internal(record: &Value) -> bool {
    record.get("internal").and_then(Value::as_bool) == Some(true)
        || clean_field(record, &["visibility"])
            .is_some_and(|value| value.eq_ignore_ascii_case("internal"))
}

fn subagent_card_id(subagent_id: &str) -> String {
    format!("subagent:{subagent_id}")
}

fn nested_clean_field(value: &Value, object_key: &str, keys: &[&str]) -> Option<String> {
    let nested = value.get(object_key)?;
    clean_field(nested, keys)
}

fn clean_field(value: &Value, keys: &[&str]) -> Option<String> {
    let object = value.as_object()?;
    keys.iter()
        .filter_map(|key| object.get(*key).and_then(Value::as_str))
        .map(str::trim)
        .find(|value| !value.is_empty())
        .map(ToOwned::to_owned)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn state_records_do_not_advance_card_count() {
        let mut index = TranscriptCardIndex::default();
        index.apply_record(0, &json!({"role":"user","text":"hi"}));
        for line in 1..101 {
            index.apply_record(line, &json!({"role":"token_usage","total":line}));
        }
        index.apply_record(101, &json!({"role":"assistant","text":"hello"}));
        assert_eq!(index.total_cards(), 2);
        assert_eq!(index.select(0, 75).runtime_state_lines, vec![100]);
    }

    #[test]
    fn agent_pty_records_reduce_to_one_card() {
        let mut index = TranscriptCardIndex::default();
        index.apply_record(
            0,
            &json!({"role":"agent_pty","event":"agent_block_begin","block_id":"block"}),
        );
        index.apply_record(
            1,
            &json!({"role":"agent_pty","event":"agent_block_delta","block_id":"block","delta":"hi"}),
        );
        index.apply_record(
            2,
            &json!({"role":"agent_pty","event":"agent_block_end","block_id":"block"}),
        );
        assert_eq!(index.total_cards(), 1);
        let recipe = index.indexed_recipe(0).unwrap();
        assert_eq!(recipe.events.len(), 3);
        assert_eq!(recipe.version, 3);
    }

    #[test]
    fn selected_subagent_child_brings_parent_inside_limit() {
        let mut index = TranscriptCardIndex::default();
        index.apply_record(
            0,
            &json!({"role":"subagent_start","id":"sub","name":"worker"}),
        );
        for line in 1..5 {
            index.apply_record(
                line,
                &json!({"role":"assistant","id":format!("msg-{line}"),"subagent_id":"sub","text":"x"}),
            );
        }
        let selection = index.select(3, 2);
        assert_eq!(selection.card_indices, vec![0, 4]);
    }

    #[test]
    fn explicit_card_identity_is_preserved_for_the_client() {
        let mut index = TranscriptCardIndex::default();
        index.apply_record(
            0,
            &json!({"role":"approval","card_id":"approval:request-1","text":"review"}),
        );
        let recipe = index.indexed_recipe(0).unwrap();
        assert_eq!(recipe.card_id, "approval:request-1");
    }

    #[test]
    fn repeated_snapshot_identity_reduces_to_latest_durable_card() {
        let mut index = TranscriptCardIndex::default();
        index.apply_record(
            0,
            &json!({"role":"diff","item_id":"patch-1","text":"first"}),
        );
        index.apply_record(
            1,
            &json!({"role":"diff","item_id":"patch-1","text":"latest"}),
        );
        assert_eq!(index.total_cards(), 1);
        let recipe = index.indexed_recipe(0).unwrap();
        assert_eq!(recipe.card_id, "diff:patch-1");
        assert_eq!(recipe.version, 2);
        assert_eq!(recipe.events.len(), 1);
        assert!(matches!(
            recipe.events[0],
            IndexedProjectionEvent::Line {
                line_index: 1,
                operation: "create"
            }
        ));
        let live_metadata = index.event_metadata(1).unwrap();
        assert_eq!(live_metadata.operation, "update");
        assert_eq!(live_metadata.version, 2);
    }

    #[test]
    fn repeated_explicit_card_identity_keeps_latest_snapshot() {
        let mut index = TranscriptCardIndex::default();
        index.apply_record(
            0,
            &json!({"role":"approval","card_id":"approval:request-1","status":"pending"}),
        );
        index.apply_record(
            1,
            &json!({"role":"approval","card_id":"approval:request-1","status":"resolved"}),
        );
        assert_eq!(index.total_cards(), 1);
        let recipe = index.indexed_recipe(0).unwrap();
        assert_eq!(recipe.version, 2);
        assert!(matches!(
            recipe.events[0],
            IndexedProjectionEvent::Line {
                line_index: 1,
                operation: "create"
            }
        ));
    }

    #[test]
    fn plan_approval_error_and_warning_have_stable_card_units() {
        let mut index = TranscriptCardIndex::default();
        index.apply_record(
            0,
            &json!({"role":"plan","steps":[{"step":"done","status":"completed"}]}),
        );
        index.apply_record(
            1,
            &json!({"role":"approval","card_id":"approval:req","status":"pending"}),
        );
        index.apply_record(
            2,
            &json!({"role":"approval","card_id":"approval:req","status":"resolved"}),
        );
        index.apply_record(3, &json!({"role":"error","text":"failed"}));
        index.apply_record(4, &json!({"role":"warning","text":"careful"}));

        assert_eq!(index.total_cards(), 4);
        let families = (0..index.total_cards())
            .map(|card_index| index.indexed_recipe(card_index).unwrap().family)
            .collect::<Vec<_>>();
        assert_eq!(families, vec!["plan", "approval", "error", "warning"]);
        assert_eq!(index.indexed_recipe(1).unwrap().events.len(), 1);
    }
}
