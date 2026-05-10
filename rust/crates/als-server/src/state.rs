use crate::adapter_process::{AdapterEventSink, AdapterSupervisor};
use crate::agent_log::AgentLogStore;
use crate::config::ServerConfig;
use crate::conversation_store::ConversationStore;
use crate::extension_registry::ExtensionRegistry;
use crate::ipc::IpcClientStore;
use crate::sidebar_ipc::SidebarIpcStore;
use anyhow::{Result, anyhow};
use serde::{Deserialize, Serialize};
use std::{
    fs,
    path::{Path, PathBuf},
    sync::{
        Arc, Mutex,
        atomic::{AtomicU64, Ordering},
    },
};
use tracing::warn;

const APP_UI_STATE_FILE: &str = "app_state.json";

#[derive(Clone)]
pub struct AppState {
    pub adapter: AdapterSupervisor,
    pub agent_log: AgentLogStore,
    pub config: ServerConfig,
    pub conversations: ConversationStore,
    pub extensions: ExtensionRegistry,
    pub host_ui: HostUiStore,
    pub ipc_clients: IpcClientStore,
    pub list_revision: Arc<AtomicU64>,
    pub sidebar_ipc: SidebarIpcStore,
    pub ui_selection: UiSelectionStore,
}

impl AppState {
    pub fn new(config: ServerConfig) -> Self {
        let events = AdapterEventSink::default();
        let adapter = AdapterSupervisor::new(config.clone(), events);
        let agent_log = AgentLogStore::with_cache_dir(config.roots.cache_dir.clone());
        let conversations = ConversationStore::new(config.roots.data_dir.clone());
        let extensions = ExtensionRegistry::load_with_config(
            config.extension_roots(),
            Some(config.roots.config_dir.clone()),
        )
        .unwrap_or_else(|error| {
            warn!(
                error = %error,
                path = %config.extensions_dir.display(),
                "failed to load ALS-RS extension registry"
            );
            ExtensionRegistry::load_empty_with_config(
                config.extension_roots(),
                Some(config.roots.config_dir.clone()),
            )
        });
        let host_ui = HostUiStore::default();
        let ipc_clients = IpcClientStore::default();
        let list_revision = Arc::new(AtomicU64::new(0));
        let sidebar_ipc = SidebarIpcStore::default();
        let ui_selection = UiSelectionStore::with_cache_dir(config.roots.cache_dir.clone());
        Self {
            adapter,
            agent_log,
            config,
            conversations,
            extensions,
            host_ui,
            ipc_clients,
            list_revision,
            sidebar_ipc,
            ui_selection,
        }
    }

    pub fn current_list_revision(&self) -> u64 {
        self.list_revision.load(Ordering::SeqCst)
    }

    pub fn bump_list_revision(&self) -> u64 {
        self.list_revision.fetch_add(1, Ordering::SeqCst) + 1
    }
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct HostUiSnapshot {
    pub show_close: bool,
    pub parent_origin: Option<String>,
    pub ide_mode: bool,
    pub project_root: Option<String>,
}

#[derive(Clone, Default)]
pub struct HostUiStore {
    inner: Arc<Mutex<HostUiSnapshot>>,
}

impl HostUiStore {
    pub fn snapshot(&self) -> Result<HostUiSnapshot> {
        let state = self
            .inner
            .lock()
            .map_err(|_| anyhow!("host UI lock poisoned"))?;
        Ok(state.clone())
    }

    pub fn set_project_root(
        &self,
        project_root: Option<String>,
        ide_mode: bool,
    ) -> Result<HostUiSnapshot> {
        let mut state = self
            .inner
            .lock()
            .map_err(|_| anyhow!("host UI lock poisoned"))?;
        state.project_root = normalized_nonempty(project_root);
        if ide_mode {
            state.ide_mode = true;
        }
        Ok(state.clone())
    }

    pub fn te2_base_url(&self) -> String {
        self.snapshot()
            .ok()
            .and_then(|snapshot| snapshot.parent_origin)
            .filter(|origin| origin.starts_with("http://") || origin.starts_with("https://"))
            .map(|origin| origin.trim_end_matches('/').to_owned())
            .unwrap_or_else(|| "http://127.0.0.1:8089".to_owned())
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct UiSelectionSnapshot {
    pub active_conversation_id: Option<String>,
    pub active_view: String,
    pub user_name: Option<String>,
    pub show_console_worker_id: bool,
}

impl Default for UiSelectionSnapshot {
    fn default() -> Self {
        Self {
            active_conversation_id: None,
            active_view: "splash".to_owned(),
            user_name: None,
            show_console_worker_id: false,
        }
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
struct PersistedAppUiState {
    #[serde(default)]
    active_conversation: Option<String>,
    #[serde(default = "default_active_view")]
    active_view: String,
    #[serde(default)]
    user_name: Option<String>,
    #[serde(default)]
    show_console_worker_id: bool,
}

impl From<UiSelectionSnapshot> for PersistedAppUiState {
    fn from(snapshot: UiSelectionSnapshot) -> Self {
        Self {
            active_conversation: snapshot.active_conversation_id,
            active_view: snapshot.active_view,
            user_name: snapshot.user_name,
            show_console_worker_id: snapshot.show_console_worker_id,
        }
    }
}

impl From<PersistedAppUiState> for UiSelectionSnapshot {
    fn from(value: PersistedAppUiState) -> Self {
        Self {
            active_conversation_id: normalized_nonempty(value.active_conversation),
            active_view: normalized_view(Some(&value.active_view))
                .unwrap_or_else(default_active_view),
            user_name: normalized_nonempty(value.user_name),
            show_console_worker_id: value.show_console_worker_id,
        }
    }
}

#[derive(Clone)]
pub struct UiSelectionStore {
    inner: Arc<Mutex<UiSelectionSnapshot>>,
    path: Option<PathBuf>,
}

impl Default for UiSelectionStore {
    fn default() -> Self {
        Self {
            inner: Arc::new(Mutex::new(UiSelectionSnapshot::default())),
            path: None,
        }
    }
}

impl UiSelectionStore {
    pub fn with_cache_dir(cache_dir: PathBuf) -> Self {
        let path = cache_dir.join(APP_UI_STATE_FILE);
        let snapshot = match read_app_ui_state(&path) {
            Ok(snapshot) => snapshot,
            Err(error) => {
                warn!(
                    error = %error,
                    path = %path.display(),
                    "failed to load ALS-RS app UI state"
                );
                UiSelectionSnapshot::default()
            }
        };
        Self {
            inner: Arc::new(Mutex::new(snapshot)),
            path: Some(path),
        }
    }

    pub fn snapshot(&self) -> Result<UiSelectionSnapshot> {
        let state = self
            .inner
            .lock()
            .map_err(|_| anyhow!("ui selection lock poisoned"))?;
        Ok(state.clone())
    }

    pub fn select(
        &self,
        conversation_id: Option<String>,
        view: Option<String>,
    ) -> Result<UiSelectionSnapshot> {
        let snapshot = {
            let mut state = self
                .inner
                .lock()
                .map_err(|_| anyhow!("ui selection lock poisoned"))?;
            state.active_conversation_id = normalized_nonempty(conversation_id);
            if let Some(view) = normalized_view(view.as_deref()) {
                state.active_view = view;
            }
            state.clone()
        };
        self.persist(&snapshot)?;
        Ok(snapshot)
    }

    pub fn set_view(&self, view: Option<String>) -> Result<UiSelectionSnapshot> {
        let snapshot = {
            let mut state = self
                .inner
                .lock()
                .map_err(|_| anyhow!("ui selection lock poisoned"))?;
            if let Some(view) = normalized_view(view.as_deref()) {
                state.active_view = view;
            }
            state.clone()
        };
        self.persist(&snapshot)?;
        Ok(snapshot)
    }

    pub fn update_app_config(
        &self,
        user_name: Option<Option<String>>,
        show_console_worker_id: Option<bool>,
    ) -> Result<UiSelectionSnapshot> {
        let snapshot = {
            let mut state = self
                .inner
                .lock()
                .map_err(|_| anyhow!("ui selection lock poisoned"))?;
            if let Some(user_name) = user_name {
                state.user_name = normalized_nonempty(user_name);
            }
            if let Some(show_console_worker_id) = show_console_worker_id {
                state.show_console_worker_id = show_console_worker_id;
            }
            state.clone()
        };
        self.persist(&snapshot)?;
        Ok(snapshot)
    }

    fn persist(&self, snapshot: &UiSelectionSnapshot) -> Result<()> {
        let Some(path) = self.path.as_ref() else {
            return Ok(());
        };
        write_app_ui_state(path, snapshot)
    }
}

fn read_app_ui_state(path: &Path) -> Result<UiSelectionSnapshot> {
    if !path.exists() {
        return Ok(UiSelectionSnapshot::default());
    }
    let raw = fs::read_to_string(path)?;
    let persisted: PersistedAppUiState = serde_json::from_str(&raw)?;
    Ok(persisted.into())
}

fn write_app_ui_state(path: &Path, snapshot: &UiSelectionSnapshot) -> Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let payload = PersistedAppUiState::from(snapshot.clone());
    fs::write(
        path,
        format!("{}\n", serde_json::to_string_pretty(&payload)?),
    )?;
    Ok(())
}

fn normalized_view(view: Option<&str>) -> Option<String> {
    view.map(str::trim)
        .filter(|value| !value.is_empty())
        .map(ToOwned::to_owned)
}

fn default_active_view() -> String {
    "splash".to_owned()
}

fn normalized_nonempty(value: Option<String>) -> Option<String> {
    value
        .map(|value| value.trim().to_owned())
        .filter(|value| !value.is_empty())
}

#[cfg(test)]
mod tests {
    use super::{UiSelectionSnapshot, UiSelectionStore};
    use std::fs;

    #[test]
    fn ui_selection_defaults_to_splash_without_conversation() {
        let store = UiSelectionStore::default();
        assert_eq!(store.snapshot().unwrap(), UiSelectionSnapshot::default());
    }

    #[test]
    fn ui_selection_preserves_active_conversation_when_only_view_changes() {
        let store = UiSelectionStore::default();
        store
            .select(Some("conv-a".to_owned()), Some("conversation".to_owned()))
            .unwrap();
        let snapshot = store.set_view(Some("splash".to_owned())).unwrap();
        assert_eq!(snapshot.active_conversation_id.as_deref(), Some("conv-a"));
        assert_eq!(snapshot.active_view, "splash");
    }

    #[test]
    fn ui_selection_persists_minimal_app_state() {
        let root = std::env::temp_dir().join(format!(
            "als-rs-ui-selection-test-{}",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_millis()
        ));
        let store = UiSelectionStore::with_cache_dir(root.clone());
        store
            .select(Some("conv-a".to_owned()), Some("conversation".to_owned()))
            .unwrap();
        store
            .update_app_config(Some(Some("  Ada  ".to_owned())), None)
            .unwrap();

        let reloaded = UiSelectionStore::with_cache_dir(root.clone())
            .snapshot()
            .unwrap();
        assert_eq!(reloaded.active_conversation_id.as_deref(), Some("conv-a"));
        assert_eq!(reloaded.active_view, "conversation");
        assert_eq!(reloaded.user_name.as_deref(), Some("Ada"));
        assert!(!reloaded.show_console_worker_id);

        let raw = fs::read_to_string(root.join("app_state.json")).unwrap();
        assert!(raw.contains("\"active_conversation\""));
        assert!(!raw.contains("active_conversation_id"));

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn ui_selection_persists_console_worker_id_display_setting() {
        let root = std::env::temp_dir().join(format!(
            "als-rs-ui-selection-console-worker-test-{}",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_millis()
        ));
        let store = UiSelectionStore::with_cache_dir(root.clone());
        store
            .update_app_config(None, Some(true))
            .expect("setting should persist");

        let reloaded = UiSelectionStore::with_cache_dir(root.clone())
            .snapshot()
            .unwrap();
        assert!(reloaded.show_console_worker_id);

        let raw = fs::read_to_string(root.join("app_state.json")).unwrap();
        assert!(raw.contains("\"show_console_worker_id\""));

        let _ = fs::remove_dir_all(root);
    }
}
