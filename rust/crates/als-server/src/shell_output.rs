//! Disk-backed shell output. The browser receives references, never growing bodies.
use anyhow::{Result, anyhow, ensure};
use serde_json::{Value, json};
use std::{
    collections::HashMap,
    fs::{self, File, OpenOptions},
    io::{BufWriter, Read, Seek, SeekFrom, Write},
    path::PathBuf,
    sync::{Arc, Mutex},
};

const FRAGMENT_BYTES: usize = 16 * 1024;
const WINDOW_BYTES: usize = 64 * 1024;
const WINDOW_LINES: usize = 200;
const INDEX_BYTES: u64 = 16;

#[derive(Default, serde::Serialize, serde::Deserialize)]
struct Sgr {
    fg: u8,
    bg: u8,
    flags: u8,
    pending: String,
}
impl Sgr {
    fn checkpoint(&self) -> [u8; 8] {
        [self.fg, self.bg, self.flags, 0, 0, 0, 0, 0]
    }
    fn prefix(bytes: [u8; 8]) -> String {
        let mut codes = vec!["0".to_owned()];
        if bytes[0] != 0 {
            codes.push(bytes[0].to_string());
        }
        if bytes[1] != 0 {
            codes.push(bytes[1].to_string());
        }
        for (bit, code) in [1, 2, 3, 4, 7].iter().enumerate() {
            if bytes[2] & (1 << bit) != 0 {
                codes.push(code.to_string());
            }
        }
        format!("\u{1b}[{}m", codes.join(";"))
    }
    fn consume(&mut self, character: char) {
        if character == '\u{1b}' {
            self.pending.clear();
            self.pending.push(character);
            return;
        }
        if self.pending.is_empty() {
            return;
        }
        self.pending.push(character);
        if character == 'm' && self.pending.starts_with("\u{1b}[") {
            for code in self.pending[2..self.pending.len() - 1].split(';') {
                let code = if code.is_empty() {
                    Some(0)
                } else {
                    code.parse::<u8>().ok()
                };
                match code {
                    Some(0) => {
                        self.fg = 0;
                        self.bg = 0;
                        self.flags = 0;
                    }
                    Some(n @ 1..=4) => self.flags |= 1 << (n - 1),
                    Some(7) => self.flags |= 16,
                    Some(22) => self.flags &= !3,
                    Some(23) => self.flags &= !4,
                    Some(24) => self.flags &= !8,
                    Some(27) => self.flags &= !16,
                    Some(39) => self.fg = 0,
                    Some(49) => self.bg = 0,
                    Some(n @ (30..=37 | 90..=97)) => self.fg = n,
                    Some(n @ (40..=47 | 100..=107)) => self.bg = n,
                    _ => {}
                }
            }
            self.pending.clear();
        } else if self.pending.len() > 64
            || (character != '[' && !character.is_ascii_digit() && character != ';')
        {
            self.pending.clear();
        }
    }
}

#[derive(Clone)]
pub struct ShellOutputStore {
    root: PathBuf,
    owners: Arc<Mutex<HashMap<(String, String), String>>>,
}

#[cfg(test)]
mod tests {
    use super::*;
    struct Fixture {
        root: PathBuf,
        store: ShellOutputStore,
    }
    impl Fixture {
        fn new() -> Self {
            let mut id = [0u8; 16];
            getrandom::fill(&mut id).unwrap();
            let root =
                std::env::temp_dir().join(format!("als-shell-{:x}", u128::from_le_bytes(id)));
            fs::create_dir_all(root.join("conversations/test")).unwrap();
            Self {
                store: ShellOutputStore::new(root.clone()),
                root,
            }
        }
        fn capture(&self, kind: &str, field: &str, text: &str) -> Value {
            let mut event = json!({"type":kind,"id":"command-1",(field):text});
            self.store.capture("test", &mut event).unwrap();
            event
        }
        fn params(event: &Value) -> Value {
            json!({"conversation_id":"test","output_id":event["shell_output"]["id"],"action":"tail"})
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.root);
        }
    }

    #[test]
    fn bounded_tail_and_independent_history_cursors() {
        let fixture = Fixture::new();
        let output = (0..10_000)
            .map(|n| format!("line {n}\n"))
            .collect::<String>();
        let event = fixture.capture("shell_delta", "delta", &output);
        assert!(event.get("delta").is_none());
        let params = Fixture::params(&event);
        let tail = fixture.store.window(&params).unwrap();
        assert!(tail["text"].as_str().unwrap().ends_with("line 9999\n"));
        assert!(tail["text"].as_str().unwrap().len() <= WINDOW_BYTES);
        assert!(tail["end"].as_u64().unwrap() - tail["start"].as_u64().unwrap() <= 200);
        let mut older_params = params.clone();
        older_params["action"] = json!("older");
        older_params["start"] = tail["start"].clone();
        let older = fixture.store.window(&older_params).unwrap();
        assert!(older["start"].as_u64().unwrap() < tail["start"].as_u64().unwrap());
        assert_eq!(fixture.store.window(&params).unwrap(), tail);
    }

    #[test]
    fn long_unicode_partial_lines_are_bounded_and_readable_after_restart() {
        let fixture = Fixture::new();
        fixture.capture("shell_delta", "delta", &"\u{1f642}".repeat(40_000));
        let event = fixture.capture("shell_delta", "delta", "tail\rprogress\n");
        let store = ShellOutputStore::new(fixture.root.clone());
        let tail = store.window(&Fixture::params(&event)).unwrap();
        assert!(tail["text"].as_str().unwrap().len() <= WINDOW_BYTES);
        assert!(tail["text"].as_str().unwrap().ends_with("tail\rprogress\n"));
        assert!(!tail["text"].as_str().unwrap().contains('\u{fffd}'));
    }

    #[test]
    fn final_snapshot_is_not_appended_twice_and_clipped_final_does_not_erase_stream() {
        let fixture = Fixture::new();
        fixture.capture("shell_delta", "delta", "one\ntwo\n");
        let end = fixture.capture("shell_end", "stdout", "one\ntwo\n");
        let record = fixture.capture("command", "output", "one\ntwo\n");
        assert_eq!(end["shell_output"]["id"], record["shell_output"]["id"]);
        fixture.capture("shell_end", "stdout", "one");
        let output = fixture.store.window(&Fixture::params(&record)).unwrap();
        assert_eq!(output["text"], "one\ntwo\n");
    }

    #[test]
    fn older_window_keeps_overlap_across_large_fragments() {
        let fixture = Fixture::new();
        let output = format!("{}{}", "x".repeat(500_000), "small\n".repeat(500));
        let event = fixture.capture("shell_end", "stdout", &output);
        let mut params = Fixture::params(&event);
        params["action"] = json!("older");
        params["start"] = json!(35);
        let window = fixture.store.window(&params).unwrap();
        assert!(window["start"].as_u64().unwrap() <= 35);
        assert!(window["end"].as_u64().unwrap() > 35);
        assert!(window["text"].as_str().unwrap().len() <= WINDOW_BYTES);
    }

    #[test]
    fn ansi_checkpoints_survive_windows_partial_deltas_and_restart() {
        let fixture = Fixture::new();
        fixture.capture("shell_delta", "delta", "\u{1b}[3");
        fixture.capture(
            "shell_delta",
            "delta",
            &format!("1m{}", "red\n".repeat(500)),
        );
        let restarted = ShellOutputStore::new(fixture.root.clone());
        let mut event = json!({"type":"shell_delta","id":"command-1","delta":"still red\n"});
        restarted.capture("test", &mut event).unwrap();
        let window = restarted.window(&Fixture::params(&event)).unwrap();
        assert_eq!(window["ansi_prefix"], "\u{1b}[0;31m");
        assert!(window["text"].as_str().unwrap().ends_with("still red\n"));
        assert!(window["start"].as_u64().unwrap() > 0);
    }

    #[test]
    fn legacy_output_is_cached_without_mutating_the_original_record() {
        let fixture = Fixture::new();
        let path = fixture.root.join("conversations/test/transcript.jsonl");
        let original = json!({"role":"command","id":"old","output":"old output\n"});
        let mut first = original.clone();
        ShellOutputStore::project_legacy(&path, 3, &mut first).unwrap();
        let mut second = original.clone();
        ShellOutputStore::project_legacy(&path, 3, &mut second).unwrap();
        assert_eq!(first, second);
        assert_eq!(
            fixture.store.window(&Fixture::params(&first)).unwrap()["text"],
            "old output\n"
        );
        assert_eq!(original["output"], "old output\n");
    }

    #[test]
    fn rejects_paths_and_unknown_outputs() {
        let fixture = Fixture::new();
        assert!(
            fixture
                .store
                .window(&json!({"conversation_id":"../test","output_id":"a".repeat(32)}))
                .is_err()
        );
        assert!(
            fixture
                .store
                .window(&json!({"conversation_id":"test","output_id":"../meta.json"}))
                .is_err()
        );
        assert!(
            fixture
                .store
                .window(&json!({"conversation_id":"test","output_id":"a".repeat(32)}))
                .is_err()
        );
    }

    #[test]
    fn stderr_boundary_survives_the_durable_final_snapshot() {
        let fixture = Fixture::new();
        let mut end = json!({"type":"shell_end","id":"command-1","stdout":"out","stderr":"error"});
        fixture.store.capture("test", &mut end).unwrap();
        let entry = fixture.capture("command", "output", "outerror");
        let window = fixture.store.window(&Fixture::params(&entry)).unwrap();
        assert_eq!(window["text"], "outerror");
        assert_eq!(window["stderr_start"], 3);
        assert_eq!(window["offsets"], json!([0, 3]));
    }
}

impl ShellOutputStore {
    pub fn project_legacy(
        transcript: &std::path::Path,
        line: usize,
        event: &mut Value,
    ) -> Result<()> {
        let kind = event.get("role").and_then(Value::as_str).unwrap_or("");
        if !matches!(kind, "command" | "shell") || event.get("shell_output").is_some() {
            return Ok(());
        }
        let directory = transcript
            .parent()
            .ok_or_else(|| anyhow!("missing conversation directory"))?;
        let conversation = directory
            .file_name()
            .and_then(|s| s.to_str())
            .ok_or_else(|| anyhow!("invalid conversation path"))?;
        let data_dir = directory
            .parent()
            .and_then(|p| p.parent())
            .ok_or_else(|| anyhow!("missing data directory"))?;
        let reference = directory
            .join("shell-output")
            .join(format!("legacy-{line}.json"));
        if reference.is_file() {
            event["shell_output"] = serde_json::from_slice(&fs::read(reference)?)?;
            // A fork may inherit cached references from its source.
            event["shell_output"]["conversation_id"] = json!(conversation);
            if let Some(object) = event.as_object_mut() {
                for key in ["stdout", "stderr", "output"] {
                    object.remove(key);
                }
            }
        } else {
            Self::new(data_dir.to_path_buf()).capture(conversation, event)?;
            fs::write(reference, serde_json::to_vec(&event["shell_output"])?)?;
        }
        Ok(())
    }

    pub fn new(data_dir: PathBuf) -> Self {
        Self {
            root: data_dir.join("conversations"),
            owners: Default::default(),
        }
    }

    fn directory(&self, conversation: &str) -> Result<PathBuf> {
        ensure!(
            !conversation.is_empty()
                && conversation
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-'),
            "invalid conversation ID"
        );
        let directory = self.root.join(conversation);
        ensure!(directory.is_dir(), "conversation not found");
        Ok(directory.join("shell-output"))
    }

    fn paths(&self, conversation: &str, id: &str) -> Result<(PathBuf, PathBuf)> {
        ensure!(
            id.len() == 32 && id.bytes().all(|b| b.is_ascii_hexdigit()),
            "invalid shell output ID"
        );
        let directory = self.directory(conversation)?;
        Ok((
            directory.join(format!("{id}.txt")),
            directory.join(format!("{id}.idx")),
        ))
    }

    /// Capture before generic card truncation. Repeated final snapshots replace,
    /// rather than append to, the accumulated stream.
    pub fn capture(&self, conversation: &str, event: &mut Value) -> Result<()> {
        let kind = event
            .get("type")
            .or_else(|| event.get("role"))
            .and_then(Value::as_str)
            .unwrap_or("");
        if !matches!(
            kind,
            "shell_begin" | "shell_delta" | "shell_end" | "command_result" | "command" | "shell"
        ) {
            return Ok(());
        }
        if event.get("shell_output").is_some() {
            return Ok(());
        }
        let delta = kind == "shell_delta";
        let begin = kind == "shell_begin";
        let identity = event
            .get("id")
            .or_else(|| event.get("item_id"))
            .or_else(|| event.get("call_id"))
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned();
        let mut owners = self
            .owners
            .lock()
            .map_err(|_| anyhow!("shell output lock poisoned"))?;
        let key = (conversation.to_owned(), identity.clone());
        let mut owner_path = self.directory(conversation)?.join("owners");
        for chunk in identity.as_bytes().chunks(64) {
            owner_path.push(chunk.iter().map(|b| format!("{b:02x}")).collect::<String>());
        }
        owner_path.push("id");
        let id = if !identity.is_empty() {
            match owners.get(&key).cloned() {
                Some(id) => Some(id),
                None => match fs::read_to_string(&owner_path) {
                    Ok(id) => Some(id),
                    Err(error) if error.kind() == std::io::ErrorKind::NotFound => None,
                    Err(error) => return Err(error.into()),
                },
            }
        } else {
            None
        }
        .unwrap_or_else(|| String::new());
        let id = if id.is_empty() {
            let mut bytes = [0u8; 16];
            getrandom::fill(&mut bytes).map_err(|e| anyhow!("shell output ID: {e}"))?;
            let id = bytes.iter().map(|b| format!("{b:02x}")).collect::<String>();
            fs::create_dir_all(self.directory(conversation)?)?;
            let (text, index) = self.paths(conversation, &id)?;
            File::create(text)?;
            File::create(index)?.write_all(&[0; INDEX_BYTES as usize])?;
            if owners.len() >= 1024 {
                owners.clear();
            }
            if !identity.is_empty() {
                fs::create_dir_all(
                    owner_path
                        .parent()
                        .ok_or_else(|| anyhow!("missing owner directory"))?,
                )?;
                fs::write(&owner_path, id.as_bytes())?;
                owners.insert(key.clone(), id.clone());
            }
            id
        } else {
            id
        };
        if !identity.is_empty() && owners.len() < 1024 {
            owners.insert(key, id.clone());
        }
        let (text_path, index_path) = self.paths(conversation, &id)?;
        let mut output = if delta {
            event.get("delta")
        } else {
            event.get("output").or_else(|| event.get("stdout"))
        }
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_owned();
        let stderr_start = (!delta
            && event
                .get("stderr")
                .and_then(Value::as_str)
                .is_some_and(|s| !s.is_empty()))
        .then_some(output.len() as u64)
        .or_else(|| {
            if !delta && event.get("stderr").is_none() {
                fs::read(text_path.with_extension("stderr"))
                    .ok()
                    .and_then(|bytes| <[u8; 8]>::try_from(bytes).ok())
                    .map(u64::from_le_bytes)
            } else {
                None
            }
        });
        if !delta {
            if let Some(stderr) = event.get("stderr").and_then(Value::as_str) {
                output.push_str(stderr);
            }
        }
        let existing = fs::metadata(&text_path)?.len();
        let sgr_path = text_path.with_extension("sgr");
        // Some providers supply only a clipped final summary. Retain the longer
        // received stream instead of destroying its history with that summary.
        if !output.is_empty() && (delta || output.len() as u64 >= existing) {
            let mut text = OpenOptions::new()
                .write(true)
                .append(delta)
                .truncate(!delta)
                .open(&text_path)?;
            let mut index = OpenOptions::new()
                .read(true)
                .write(true)
                .open(&index_path)?;
            if !delta {
                index.set_len(INDEX_BYTES)?;
            }
            let mut sgr: Sgr = if delta && sgr_path.is_file() {
                serde_json::from_slice(&fs::read(&sgr_path)?)?
            } else {
                Sgr::default()
            };
            let base = if delta { existing } else { 0 };
            index.seek(SeekFrom::End(-(INDEX_BYTES as i64)))?;
            let mut bytes = [0; 8];
            index.read_exact(&mut bytes)?;
            let mut fragment_start = u64::from_le_bytes(bytes);
            index.seek(SeekFrom::End(0))?;
            let mut index = BufWriter::new(index);
            for (position, character) in output.char_indices() {
                let offset = base + position as u64;
                if stderr_start == Some(offset) && !delta {
                    sgr = Sgr::default();
                    if offset != fragment_start {
                        index.write_all(&offset.to_le_bytes())?;
                        index.write_all(&sgr.checkpoint())?;
                        fragment_start = offset;
                    }
                }
                if offset.saturating_sub(fragment_start) >= FRAGMENT_BYTES as u64
                    && sgr.pending.is_empty()
                {
                    index.write_all(&offset.to_le_bytes())?;
                    index.write_all(&sgr.checkpoint())?;
                    fragment_start = offset;
                }
                sgr.consume(character);
                if character == '\n' {
                    fragment_start = offset + 1;
                    index.write_all(&fragment_start.to_le_bytes())?;
                    index.write_all(&sgr.checkpoint())?;
                }
            }
            text.write_all(output.as_bytes())?;
            index.flush()?;
            fs::write(sgr_path, serde_json::to_vec(&sgr)?)?;
            if let Some(offset) = stderr_start {
                fs::write(text_path.with_extension("stderr"), offset.to_le_bytes())?;
            } else if !delta && event.get("stderr").is_some() {
                match fs::remove_file(text_path.with_extension("stderr")) {
                    Ok(()) => {}
                    Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
                    Err(error) => return Err(error.into()),
                }
            }
        }
        let bytes = fs::metadata(&text_path)?.len();
        let object = event
            .as_object_mut()
            .ok_or_else(|| anyhow!("shell event must be an object"))?;
        for field in ["output", "stdout", "stderr", "delta"] {
            object.remove(field);
        }
        object.insert(
            "shell_output".into(),
            json!({"id":id,"conversation_id":conversation,"bytes":bytes,"running":begin || delta}),
        );
        Ok(())
    }

    pub fn window(&self, params: &Value) -> Result<Value> {
        let conversation = params
            .get("conversation_id")
            .and_then(Value::as_str)
            .unwrap_or("");
        let id = params
            .get("output_id")
            .and_then(Value::as_str)
            .unwrap_or("");
        let _guard = self
            .owners
            .lock()
            .map_err(|_| anyhow!("shell output lock poisoned"))?;
        let (text_path, index_path) = self.paths(conversation, id)?;
        let stderr_start = fs::read(text_path.with_extension("stderr"))
            .ok()
            .and_then(|bytes| <[u8; 8]>::try_from(bytes).ok())
            .map(u64::from_le_bytes);
        let mut text = File::open(text_path)?;
        let mut index = File::open(index_path)?;
        let bytes = text.metadata()?.len();
        let count = index.metadata()?.len() / INDEX_BYTES;
        let mut offset = |position: u64| -> Result<u64> {
            if position >= count {
                return Ok(bytes);
            }
            index.seek(SeekFrom::Start(position * INDEX_BYTES))?;
            let mut buffer = [0; 8];
            index.read_exact(&mut buffer)?;
            Ok(u64::from_le_bytes(buffer))
        };
        let tail = params
            .get("action")
            .and_then(Value::as_str)
            .unwrap_or("tail")
            == "tail";
        let requested = params
            .get("start")
            .and_then(Value::as_u64)
            .unwrap_or(0)
            .min(count.saturating_sub(1));
        let action = params
            .get("action")
            .and_then(Value::as_str)
            .unwrap_or("tail");
        let shift = params
            .get("shift")
            .and_then(Value::as_u64)
            .unwrap_or(50)
            .clamp(1, 50);
        let mut start = match action {
            "tail" => count.saturating_sub(WINDOW_LINES as u64),
            "older" => requested.saturating_sub(shift),
            "newer" => (requested + shift).min(count.saturating_sub(1)),
            "current" => requested,
            _ => return Err(anyhow!("invalid shell window action")),
        };
        let mut end = (start + WINDOW_LINES as u64).min(count);
        if action == "older" {
            // Always retain the previous leading fragment as the scroll anchor,
            // even when earlier fragments are much larger than the old window.
            end = (requested + 1).min(count);
            while start + 1 < end
                && offset(end)?.saturating_sub(offset(start)?) > WINDOW_BYTES as u64
            {
                start += 1;
            }
            while end < count
                && end - start < WINDOW_LINES as u64
                && offset(end + 1)?.saturating_sub(offset(start)?) <= WINDOW_BYTES as u64
            {
                end += 1;
            }
        }
        if tail {
            while start + 1 < end
                && offset(end)?.saturating_sub(offset(start)?) > WINDOW_BYTES as u64
            {
                start += 1;
            }
        } else {
            while end > start + 1
                && offset(end)?.saturating_sub(offset(start)?) > WINDOW_BYTES as u64
            {
                end -= 1;
            }
        }
        let first = offset(start)?;
        let last = offset(end)?;
        let offsets = (start..end).map(&mut offset).collect::<Result<Vec<_>>>()?;
        let mut ansi = [0; 8];
        index.seek(SeekFrom::Start(start * INDEX_BYTES + 8))?;
        index.read_exact(&mut ansi)?;
        ensure!(
            last.saturating_sub(first) <= WINDOW_BYTES as u64,
            "shell window exceeds byte limit"
        );
        let mut buffer = vec![0; (last - first) as usize];
        text.seek(SeekFrom::Start(first))?;
        text.read_exact(&mut buffer)?;
        Ok(
            json!({"output_id":id,"conversation_id":conversation,"text":String::from_utf8(buffer)?,"ansi_prefix":Sgr::prefix(ansi),"stderr_start":stderr_start,"offsets":offsets,"start":start,"end":end,"total":count,"start_byte":first,"end_byte":last,"bytes":bytes,"at_start":first==0,"at_tail":last==bytes,"unit":"line_fragment"}),
        )
    }
}
