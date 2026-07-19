use anyhow::{Result, anyhow};
use serde::{Deserialize, Serialize};
use std::{
    collections::HashMap,
    sync::{
        Arc, Mutex,
        atomic::{AtomicU64, Ordering},
    },
    time::{SystemTime, UNIX_EPOCH},
};

#[derive(Clone, Copy, Debug, Default, Deserialize, Eq, PartialEq, Serialize)]
pub struct ComposerSelection {
    pub anchor: usize,
    pub focus: usize,
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct ComposerSyncSnapshot {
    pub owner_socket_id: Option<String>,
    pub origin_client_id: Option<String>,
    pub selection: Option<ComposerSelection>,
    pub selection_revision: u64,
    pub draft_revision: u64,
    pub client_sequence: u64,
}

#[derive(Clone, Default)]
pub struct ComposerSyncStore {
    inner: Arc<Mutex<ComposerSyncState>>,
    operation_counter: Arc<AtomicU64>,
}

#[derive(Default)]
struct ComposerSyncState {
    conversations: HashMap<String, ComposerSyncSnapshot>,
    socket_conversations: HashMap<String, String>,
    socket_clients: HashMap<String, String>,
    client_sequences: HashMap<String, u64>,
}

impl ComposerSyncStore {
    pub fn note_selection(
        &self,
        conversation_id: &str,
        socket_id: &str,
        client_id: &str,
        client_sequence: u64,
        selection: ComposerSelection,
    ) -> Result<Option<ComposerSyncSnapshot>> {
        let mut state = self.lock()?;
        if !accept_client_sequence(&mut state, client_id, client_sequence) {
            return Ok(None);
        }
        move_socket_owner(&mut state, socket_id, conversation_id);
        state
            .socket_clients
            .insert(socket_id.to_owned(), client_id.to_owned());
        let snapshot = state
            .conversations
            .entry(conversation_id.to_owned())
            .or_default();
        snapshot.owner_socket_id = Some(socket_id.to_owned());
        snapshot.origin_client_id = Some(client_id.to_owned());
        snapshot.selection = Some(selection);
        snapshot.selection_revision = snapshot.selection_revision.saturating_add(1);
        snapshot.client_sequence = client_sequence;
        Ok(Some(snapshot.clone()))
    }

    pub fn note_draft(
        &self,
        conversation_id: &str,
        socket_id: &str,
        client_id: &str,
        client_sequence: u64,
        selection: Option<ComposerSelection>,
        draft_revision: u64,
    ) -> Result<ComposerSyncSnapshot> {
        let mut state = self.lock()?;
        let sequence_is_current = accept_client_sequence(&mut state, client_id, client_sequence);
        move_socket_owner(&mut state, socket_id, conversation_id);
        state
            .socket_clients
            .insert(socket_id.to_owned(), client_id.to_owned());
        let snapshot = state
            .conversations
            .entry(conversation_id.to_owned())
            .or_default();
        if draft_revision >= snapshot.draft_revision {
            snapshot.draft_revision = draft_revision;
            snapshot.owner_socket_id = Some(socket_id.to_owned());
            snapshot.origin_client_id = Some(client_id.to_owned());
            if sequence_is_current {
                if let Some(selection) = selection {
                    snapshot.selection = Some(selection);
                    snapshot.selection_revision = snapshot.selection_revision.saturating_add(1);
                }
                snapshot.client_sequence = client_sequence;
            }
        }
        Ok(snapshot.clone())
    }

    pub fn note_server_draft(
        &self,
        conversation_id: &str,
        selection: ComposerSelection,
        draft_revision: u64,
    ) -> Result<ComposerSyncSnapshot> {
        let mut state = self.lock()?;
        let snapshot = state
            .conversations
            .entry(conversation_id.to_owned())
            .or_default();
        snapshot.draft_revision = snapshot.draft_revision.max(draft_revision);
        snapshot.owner_socket_id = None;
        snapshot.origin_client_id = None;
        snapshot.selection = Some(selection);
        snapshot.selection_revision = snapshot.selection_revision.saturating_add(1);
        snapshot.client_sequence = 0;
        Ok(snapshot.clone())
    }

    pub fn snapshot(&self, conversation_id: &str) -> Result<Option<ComposerSyncSnapshot>> {
        Ok(self.lock()?.conversations.get(conversation_id).cloned())
    }

    pub fn owner_socket_id(&self, conversation_id: &str) -> Result<Option<String>> {
        Ok(self
            .snapshot(conversation_id)?
            .and_then(|snapshot| snapshot.owner_socket_id))
    }

    pub fn remove_socket(&self, socket_id: &str) -> Result<()> {
        let mut state = self.lock()?;
        if let Some(client_id) = state.socket_clients.remove(socket_id) {
            state.client_sequences.remove(&client_id);
        }
        if let Some(conversation_id) = state.socket_conversations.remove(socket_id)
            && let Some(snapshot) = state.conversations.get_mut(&conversation_id)
            && snapshot.owner_socket_id.as_deref() == Some(socket_id)
        {
            snapshot.owner_socket_id = None;
        }
        Ok(())
    }

    pub fn next_operation_id(&self) -> String {
        let millis = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis();
        let counter = self.operation_counter.fetch_add(1, Ordering::Relaxed) + 1;
        format!("mention_{millis}_{counter}")
    }

    fn lock(&self) -> Result<std::sync::MutexGuard<'_, ComposerSyncState>> {
        self.inner
            .lock()
            .map_err(|_| anyhow!("composer sync lock poisoned"))
    }
}

fn accept_client_sequence(
    state: &mut ComposerSyncState,
    client_id: &str,
    client_sequence: u64,
) -> bool {
    let previous = state
        .client_sequences
        .get(client_id)
        .copied()
        .unwrap_or_default();
    if client_sequence > 0 && client_sequence <= previous {
        return false;
    }
    state
        .client_sequences
        .insert(client_id.to_owned(), client_sequence.max(previous));
    true
}

fn move_socket_owner(state: &mut ComposerSyncState, socket_id: &str, conversation_id: &str) {
    if let Some(previous_conversation_id) = state
        .socket_conversations
        .insert(socket_id.to_owned(), conversation_id.to_owned())
        && previous_conversation_id != conversation_id
        && let Some(previous) = state.conversations.get_mut(&previous_conversation_id)
        && previous.owner_socket_id.as_deref() == Some(socket_id)
    {
        previous.owner_socket_id = None;
    }
}

#[cfg(test)]
mod tests {
    use super::{ComposerSelection, ComposerSyncStore};

    #[test]
    fn newest_local_activity_owns_the_conversation() {
        let store = ComposerSyncStore::default();
        store
            .note_selection(
                "conv-a",
                "socket-a",
                "client-a",
                1,
                ComposerSelection {
                    anchor: 4,
                    focus: 4,
                },
            )
            .unwrap();
        store
            .note_selection(
                "conv-a",
                "socket-b",
                "client-b",
                1,
                ComposerSelection {
                    anchor: 8,
                    focus: 8,
                },
            )
            .unwrap();

        let snapshot = store.snapshot("conv-a").unwrap().unwrap();
        assert_eq!(snapshot.owner_socket_id.as_deref(), Some("socket-b"));
        assert_eq!(snapshot.origin_client_id.as_deref(), Some("client-b"));
        assert_eq!(snapshot.selection.unwrap().focus, 8);
    }

    #[test]
    fn moving_and_disconnecting_a_socket_clears_stale_ownership() {
        let store = ComposerSyncStore::default();
        store
            .note_selection(
                "conv-a",
                "socket-a",
                "client-a",
                1,
                ComposerSelection::default(),
            )
            .unwrap();
        store
            .note_selection(
                "conv-b",
                "socket-a",
                "client-a",
                2,
                ComposerSelection::default(),
            )
            .unwrap();
        assert_eq!(store.owner_socket_id("conv-a").unwrap(), None);
        assert_eq!(
            store.owner_socket_id("conv-b").unwrap().as_deref(),
            Some("socket-a")
        );

        store.remove_socket("socket-a").unwrap();
        assert_eq!(store.owner_socket_id("conv-b").unwrap(), None);
    }

    #[test]
    fn stale_client_sequences_do_not_rewind_selection() {
        let store = ComposerSyncStore::default();
        store
            .note_selection(
                "conv-a",
                "socket-a",
                "client-a",
                2,
                ComposerSelection {
                    anchor: 9,
                    focus: 9,
                },
            )
            .unwrap();
        assert!(
            store
                .note_selection(
                    "conv-a",
                    "socket-a",
                    "client-a",
                    1,
                    ComposerSelection {
                        anchor: 2,
                        focus: 2,
                    },
                )
                .unwrap()
                .is_none()
        );
        assert_eq!(
            store
                .snapshot("conv-a")
                .unwrap()
                .unwrap()
                .selection
                .unwrap()
                .focus,
            9
        );
    }

    #[test]
    fn server_draft_fallback_clears_target_owner() {
        let store = ComposerSyncStore::default();
        store
            .note_selection(
                "conv-a",
                "socket-a",
                "client-a",
                1,
                ComposerSelection {
                    anchor: 3,
                    focus: 3,
                },
            )
            .unwrap();

        let snapshot = store
            .note_server_draft(
                "conv-a",
                ComposerSelection {
                    anchor: 12,
                    focus: 12,
                },
                4,
            )
            .unwrap();
        assert_eq!(snapshot.owner_socket_id, None);
        assert_eq!(snapshot.origin_client_id, None);
        assert_eq!(snapshot.draft_revision, 4);
        assert_eq!(snapshot.selection.unwrap().focus, 12);
    }
}
