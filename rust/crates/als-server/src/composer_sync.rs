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
    pub owner_client_id: Option<String>,
    pub author_epoch: u64,
    pub origin_client_id: Option<String>,
    pub selection: Option<ComposerSelection>,
    pub selection_revision: u64,
    pub draft_revision: u64,
    pub client_sequence: u64,
}

#[derive(Clone, Default)]
pub struct ComposerSyncStore {
    inner: Arc<Mutex<ComposerSyncState>>,
    mutation_gate: Arc<Mutex<()>>,
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
    pub fn mutation_guard(&self) -> Result<std::sync::MutexGuard<'_, ()>> {
        self.mutation_gate
            .lock()
            .map_err(|_| anyhow!("composer mutation gate poisoned"))
    }

    pub fn author_write_is_current(
        &self,
        conversation_id: &str,
        socket_id: &str,
        client_id: &str,
        client_sequence: u64,
        author_epoch: u64,
    ) -> Result<bool> {
        let state = self.lock()?;
        Ok(snapshot_author_write_is_current(
            &state,
            conversation_id,
            socket_id,
            client_id,
            client_sequence,
            author_epoch,
        ))
    }

    pub fn claim_author(
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
        if snapshot.owner_socket_id.as_deref() != Some(socket_id)
            || snapshot.owner_client_id.as_deref() != Some(client_id)
        {
            snapshot.author_epoch = snapshot.author_epoch.saturating_add(1);
        }
        snapshot.owner_socket_id = Some(socket_id.to_owned());
        snapshot.owner_client_id = Some(client_id.to_owned());
        snapshot.origin_client_id = Some(client_id.to_owned());
        snapshot.selection = Some(selection);
        snapshot.selection_revision = snapshot.selection_revision.saturating_add(1);
        snapshot.client_sequence = client_sequence;
        Ok(Some(snapshot.clone()))
    }

    pub fn note_selection(
        &self,
        conversation_id: &str,
        socket_id: &str,
        client_id: &str,
        client_sequence: u64,
        author_epoch: u64,
        selection: ComposerSelection,
    ) -> Result<Option<ComposerSyncSnapshot>> {
        let mut state = self.lock()?;
        if !snapshot_author_write_is_current(
            &state,
            conversation_id,
            socket_id,
            client_id,
            client_sequence,
            author_epoch,
        ) || !accept_client_sequence(&mut state, client_id, client_sequence)
        {
            return Ok(None);
        }
        let snapshot = state
            .conversations
            .entry(conversation_id.to_owned())
            .or_default();
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
        author_epoch: u64,
        selection: Option<ComposerSelection>,
        draft_revision: u64,
    ) -> Result<Option<ComposerSyncSnapshot>> {
        let mut state = self.lock()?;
        if !snapshot_author_write_is_current(
            &state,
            conversation_id,
            socket_id,
            client_id,
            client_sequence,
            author_epoch,
        ) || !accept_client_sequence(&mut state, client_id, client_sequence)
        {
            return Ok(None);
        }
        let snapshot = state
            .conversations
            .entry(conversation_id.to_owned())
            .or_default();
        if draft_revision >= snapshot.draft_revision {
            snapshot.draft_revision = draft_revision;
            snapshot.origin_client_id = Some(client_id.to_owned());
            if let Some(selection) = selection {
                snapshot.selection = Some(selection);
                snapshot.selection_revision = snapshot.selection_revision.saturating_add(1);
            }
            snapshot.client_sequence = client_sequence;
        }
        Ok(Some(snapshot.clone()))
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
        snapshot.owner_client_id = None;
        snapshot.author_epoch = snapshot.author_epoch.saturating_add(1);
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
            snapshot.owner_client_id = None;
            snapshot.author_epoch = snapshot.author_epoch.saturating_add(1);
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

fn snapshot_author_write_is_current(
    state: &ComposerSyncState,
    conversation_id: &str,
    socket_id: &str,
    client_id: &str,
    client_sequence: u64,
    author_epoch: u64,
) -> bool {
    let previous_sequence = state
        .client_sequences
        .get(client_id)
        .copied()
        .unwrap_or_default();
    let Some(snapshot) = state.conversations.get(conversation_id) else {
        return false;
    };
    snapshot.owner_socket_id.as_deref() == Some(socket_id)
        && snapshot.owner_client_id.as_deref() == Some(client_id)
        && snapshot.author_epoch > 0
        && snapshot.author_epoch == author_epoch
        && client_sequence > previous_sequence
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
        previous.owner_client_id = None;
        previous.author_epoch = previous.author_epoch.saturating_add(1);
    }
}

#[cfg(test)]
mod tests {
    use super::{ComposerSelection, ComposerSyncStore};

    #[test]
    fn explicit_claim_moves_conversation_authorship() {
        let store = ComposerSyncStore::default();
        let first = store
            .claim_author(
                "conv-a",
                "socket-a",
                "client-a",
                1,
                ComposerSelection {
                    anchor: 4,
                    focus: 4,
                },
            )
            .unwrap()
            .unwrap();
        let second = store
            .claim_author(
                "conv-a",
                "socket-b",
                "client-b",
                1,
                ComposerSelection {
                    anchor: 8,
                    focus: 8,
                },
            )
            .unwrap()
            .unwrap();

        let snapshot = store.snapshot("conv-a").unwrap().unwrap();
        assert_eq!(first.author_epoch, 1);
        assert_eq!(second.author_epoch, 2);
        assert_eq!(snapshot.owner_socket_id.as_deref(), Some("socket-b"));
        assert_eq!(snapshot.owner_client_id.as_deref(), Some("client-b"));
        assert_eq!(snapshot.origin_client_id.as_deref(), Some("client-b"));
        assert_eq!(snapshot.selection.unwrap().focus, 8);
    }

    #[test]
    fn moving_and_disconnecting_a_socket_clears_stale_ownership() {
        let store = ComposerSyncStore::default();
        store
            .claim_author(
                "conv-a",
                "socket-a",
                "client-a",
                1,
                ComposerSelection::default(),
            )
            .unwrap();
        store
            .claim_author(
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
        let snapshot = store.snapshot("conv-b").unwrap().unwrap();
        assert_eq!(snapshot.owner_socket_id, None);
        assert_eq!(snapshot.owner_client_id, None);
        assert_eq!(snapshot.author_epoch, 2);
    }

    #[test]
    fn passive_client_cannot_publish_or_take_authorship() {
        let store = ComposerSyncStore::default();
        let claimed = store
            .claim_author(
                "conv-a",
                "socket-a",
                "client-a",
                1,
                ComposerSelection {
                    anchor: 9,
                    focus: 9,
                },
            )
            .unwrap()
            .unwrap();
        assert!(
            store
                .note_selection(
                    "conv-a",
                    "socket-b",
                    "client-b",
                    1,
                    claimed.author_epoch,
                    ComposerSelection {
                        anchor: 2,
                        focus: 2,
                    },
                )
                .unwrap()
                .is_none()
        );
        let snapshot = store.snapshot("conv-a").unwrap().unwrap();
        assert_eq!(snapshot.owner_client_id.as_deref(), Some("client-a"));
        assert_eq!(snapshot.selection.unwrap().focus, 9);
    }

    #[test]
    fn superseded_author_epoch_rejects_delayed_draft_and_selection() {
        let store = ComposerSyncStore::default();
        let first = store
            .claim_author(
                "conv-a",
                "socket-a",
                "client-a",
                1,
                ComposerSelection {
                    anchor: 3,
                    focus: 3,
                },
            )
            .unwrap()
            .unwrap();
        let second = store
            .claim_author(
                "conv-a",
                "socket-b",
                "client-b",
                1,
                ComposerSelection {
                    anchor: 8,
                    focus: 8,
                },
            )
            .unwrap()
            .unwrap();

        assert!(
            store
                .note_selection(
                    "conv-a",
                    "socket-a",
                    "client-a",
                    2,
                    first.author_epoch,
                    ComposerSelection {
                        anchor: 4,
                        focus: 4,
                    },
                )
                .unwrap()
                .is_none()
        );
        assert!(
            store
                .note_draft(
                    "conv-a",
                    "socket-a",
                    "client-a",
                    3,
                    first.author_epoch,
                    Some(ComposerSelection {
                        anchor: 5,
                        focus: 5,
                    }),
                    2,
                )
                .unwrap()
                .is_none()
        );
        let snapshot = store.snapshot("conv-a").unwrap().unwrap();
        assert_eq!(snapshot.author_epoch, second.author_epoch);
        assert_eq!(snapshot.owner_client_id.as_deref(), Some("client-b"));
        assert_eq!(snapshot.selection.unwrap().focus, 8);
    }

    #[test]
    fn server_draft_fallback_clears_target_owner() {
        let store = ComposerSyncStore::default();
        let claimed = store
            .claim_author(
                "conv-a",
                "socket-a",
                "client-a",
                1,
                ComposerSelection {
                    anchor: 3,
                    focus: 3,
                },
            )
            .unwrap()
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
        assert_eq!(snapshot.owner_client_id, None);
        assert_eq!(snapshot.origin_client_id, None);
        assert_eq!(snapshot.author_epoch, claimed.author_epoch + 1);
        assert_eq!(snapshot.draft_revision, 4);
        assert_eq!(snapshot.selection.unwrap().focus, 12);
    }
}
