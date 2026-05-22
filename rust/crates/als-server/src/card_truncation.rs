use regex::Regex;
use serde_json::{Map, Number, Value};
use std::sync::OnceLock;

pub const MAX_CARD_OUTPUT_BYTES: usize = 16 * 1024;
pub const MAX_SEARCH_ROW_BYTES: usize = 1024;

const TRUNCATED_KEY: &str = "truncated";
const TRUNCATION_NOTE_KEY: &str = "truncation_note";

pub fn sanitize_live_card_event(value: &mut Value) {
    let Some(object) = value.as_object_mut() else {
        return;
    };
    let event_type = object
        .get("type")
        .and_then(Value::as_str)
        .map(normalize_kind)
        .unwrap_or_default();
    sanitize_card_object(object, &event_type);
}

pub fn sanitize_transcript_card_entry(value: &mut Value) {
    let Some(object) = value.as_object_mut() else {
        return;
    };
    let role = object
        .get("role")
        .and_then(Value::as_str)
        .map(normalize_kind)
        .unwrap_or_default();
    sanitize_card_object(object, &role);
}

fn sanitize_card_object(object: &mut Map<String, Value>, kind: &str) {
    match kind {
        "command" | "command_result" | "shell_end" | "shell_delta" => {
            sanitize_text_field(object, "output", MAX_CARD_OUTPUT_BYTES);
            sanitize_text_field(object, "stdout", MAX_CARD_OUTPUT_BYTES);
            sanitize_text_field(object, "stderr", MAX_CARD_OUTPUT_BYTES);
            sanitize_text_field(object, "delta", MAX_CARD_OUTPUT_BYTES);
        }
        "view" => sanitize_view(object),
        "search" | "web_search" => sanitize_search(object),
        "tool" | "mcp_tool" | "tool_end" | "tool_delta" | "tool_interaction" => {
            sanitize_text_field(object, "output", MAX_CARD_OUTPUT_BYTES);
            sanitize_text_field(object, "delta", MAX_CARD_OUTPUT_BYTES);
            sanitize_output_value_field(object, "result", MAX_CARD_OUTPUT_BYTES);
            sanitize_output_value_field(object, "response", MAX_CARD_OUTPUT_BYTES);
        }
        _ => {}
    }
}

fn sanitize_view(object: &mut Map<String, Value>) {
    sanitize_text_field(object, "content", MAX_CARD_OUTPUT_BYTES);
    let Some(lines) = object.get_mut("lines").and_then(Value::as_array_mut) else {
        return;
    };

    let mut total = 0usize;
    let mut keep = lines.len();
    let mut truncated = false;
    for (idx, line) in lines.iter_mut().enumerate() {
        let Some(line_object) = line.as_object_mut() else {
            continue;
        };
        let Some(content_value) = line_object.get_mut("content") else {
            continue;
        };
        let Some(content) = content_value.as_str() else {
            continue;
        };
        let remaining = MAX_CARD_OUTPUT_BYTES.saturating_sub(total);
        if remaining == 0 {
            keep = idx;
            truncated = true;
            break;
        }
        let original_len = content.len();
        let capped = truncate_text_with_note(content, remaining, "line");
        total += capped.text.len();
        if capped.truncated {
            *content_value = Value::String(capped.text);
            keep = idx + 1;
            truncated = true;
            break;
        }
        total += 1;
        if original_len > remaining {
            keep = idx + 1;
            truncated = true;
            break;
        }
    }
    if keep < lines.len() {
        lines.truncate(keep);
        truncated = true;
    }
    if truncated {
        mark_truncated(
            object,
            format!(
                "view output truncated to {} KiB",
                MAX_CARD_OUTPUT_BYTES / 1024
            ),
        );
    }
}

fn sanitize_search(object: &mut Map<String, Value>) {
    let Some(text) = object.get("content").and_then(Value::as_str) else {
        return;
    };
    let capped = truncate_search_content(text, MAX_CARD_OUTPUT_BYTES, MAX_SEARCH_ROW_BYTES);
    if !capped.truncated {
        return;
    }
    object.insert("content".to_owned(), Value::String(capped.text));
    mark_truncated(
        object,
        format!(
            "search output truncated to {} KiB total / {} KiB per row",
            MAX_CARD_OUTPUT_BYTES / 1024,
            MAX_SEARCH_ROW_BYTES / 1024
        ),
    );
}

fn sanitize_text_field(object: &mut Map<String, Value>, key: &str, max_bytes: usize) {
    let Some(text) = object.get(key).and_then(Value::as_str) else {
        return;
    };
    let capped = truncate_text_with_note(text, max_bytes, key);
    if !capped.truncated {
        return;
    }
    object.insert(key.to_owned(), Value::String(capped.text));
    mark_truncated(
        object,
        format!("{} truncated to {} KiB", key, max_bytes / 1024),
    );
}

fn sanitize_output_value_field(object: &mut Map<String, Value>, key: &str, max_bytes: usize) {
    let Some(value) = object.get_mut(key) else {
        return;
    };
    if !sanitize_output_value(value, max_bytes) {
        return;
    }
    mark_truncated(
        object,
        format!("{} truncated to {} KiB", key, max_bytes / 1024),
    );
}

fn sanitize_output_value(value: &mut Value, max_bytes: usize) -> bool {
    match value {
        Value::String(text) => {
            let capped = truncate_text_with_note(text, max_bytes, "output");
            if capped.truncated {
                *text = capped.text;
                return true;
            }
            false
        }
        Value::Array(items) => {
            let mut changed = false;
            for item in items.iter_mut() {
                changed |= sanitize_output_value(item, max_bytes);
            }
            changed | cap_structured_output(value, max_bytes)
        }
        Value::Object(map) => {
            let mut changed = false;
            for child in map.values_mut() {
                changed |= sanitize_output_value(child, max_bytes);
            }
            changed | cap_structured_output(value, max_bytes)
        }
        _ => false,
    }
}

fn cap_structured_output(value: &mut Value, max_bytes: usize) -> bool {
    let Ok(serialized) = serde_json::to_string(value) else {
        return false;
    };
    if serialized.len() <= max_bytes {
        return false;
    }
    let original_bytes = serialized.len();
    let capped = truncate_text_with_note(&serialized, max_bytes, "output");
    let mut replacement = Map::new();
    replacement.insert("truncated".to_owned(), Value::Bool(true));
    replacement.insert(
        "original_bytes".to_owned(),
        Value::Number(Number::from(original_bytes as u64)),
    );
    replacement.insert("preview".to_owned(), Value::String(capped.text));
    *value = Value::Object(replacement);
    true
}

fn truncate_search_content(
    text: &str,
    max_total_bytes: usize,
    max_row_bytes: usize,
) -> TruncatedText {
    if text.is_empty() {
        return TruncatedText::unchanged(text);
    }
    let had_trailing_newline = text.ends_with('\n');
    let mut out = String::new();
    let mut truncated = false;
    let line_count = text.split('\n').count();

    for (idx, raw_line) in text.split('\n').enumerate() {
        if had_trailing_newline && idx + 1 == line_count && raw_line.is_empty() {
            break;
        }
        let capped_line = truncate_search_line(raw_line, max_row_bytes);
        truncated |= capped_line.truncated;
        let separator_bytes = usize::from(!out.is_empty());
        if out.len() + separator_bytes + capped_line.text.len() > max_total_bytes {
            truncated = true;
            break;
        }
        if !out.is_empty() {
            out.push('\n');
        }
        out.push_str(&capped_line.text);
    }

    if !truncated {
        return TruncatedText::unchanged(text);
    }
    TruncatedText {
        text: out,
        truncated: true,
    }
}

fn truncate_search_line(line: &str, max_bytes: usize) -> TruncatedText {
    if line.len() <= max_bytes {
        return TruncatedText::unchanged(line);
    }
    let regex = search_line_regex();
    let Some(captures) = regex.captures(line) else {
        return truncate_text_with_note(line, max_bytes, "row");
    };
    let Some(prefix) = captures.get(1) else {
        return truncate_text_with_note(line, max_bytes, "row");
    };
    let Some(preview) = captures.get(2) else {
        return truncate_text_with_note(line, max_bytes, "row");
    };
    let prefix_text = prefix.as_str();
    if prefix_text.len() >= max_bytes {
        return truncate_text_with_note(line, max_bytes, "row");
    }
    let capped_preview =
        truncate_text_with_note(preview.as_str(), max_bytes - prefix_text.len(), "row");
    if !capped_preview.truncated {
        return TruncatedText::unchanged(line);
    }
    TruncatedText {
        text: format!("{}{}", prefix_text, capped_preview.text),
        truncated: true,
    }
}

fn truncate_text_with_note(text: &str, max_bytes: usize, label: &str) -> TruncatedText {
    if text.len() <= max_bytes {
        return TruncatedText::unchanged(text);
    }
    let note = format!(
        "\n... ({} truncated, showing at most {} of {} bytes)",
        label,
        max_bytes,
        text.len()
    );
    let content_limit = max_bytes.saturating_sub(note.len());
    let mut truncated = truncate_at_char_boundary(text, content_limit);
    truncated.push_str(&note);
    if truncated.len() > max_bytes {
        truncated = truncate_at_char_boundary(&truncated, max_bytes);
    }
    TruncatedText {
        text: truncated,
        truncated: true,
    }
}

fn truncate_at_char_boundary(text: &str, max_bytes: usize) -> String {
    if text.len() <= max_bytes {
        return text.to_owned();
    }
    let mut end = max_bytes;
    while end > 0 && !text.is_char_boundary(end) {
        end -= 1;
    }
    text[..end].to_owned()
}

fn mark_truncated(object: &mut Map<String, Value>, note: String) {
    object.insert(TRUNCATED_KEY.to_owned(), Value::Bool(true));
    object.insert(TRUNCATION_NOTE_KEY.to_owned(), Value::String(note));
}

fn normalize_kind(value: &str) -> String {
    value.trim().to_ascii_lowercase()
}

fn search_line_regex() -> &'static Regex {
    static SEARCH_LINE_RE: OnceLock<Regex> = OnceLock::new();
    SEARCH_LINE_RE
        .get_or_init(|| Regex::new(r"^(.+?:\d+(?::\d+)?:)(.*)$").expect("valid search line regex"))
}

struct TruncatedText {
    text: String,
    truncated: bool,
}

impl TruncatedText {
    fn unchanged(text: &str) -> Self {
        Self {
            text: text.to_owned(),
            truncated: false,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn truncates_command_output_by_bytes() {
        let mut event = json!({
            "type": "command_result",
            "conversation_id": "conv",
            "output": "a".repeat(MAX_CARD_OUTPUT_BYTES + 100),
        });
        sanitize_live_card_event(&mut event);
        let output = event["output"].as_str().unwrap();
        assert!(output.len() <= MAX_CARD_OUTPUT_BYTES);
        assert!(output.contains("output truncated"));
        assert_eq!(event["truncated"], true);
    }

    #[test]
    fn truncates_search_rows_and_total_output() {
        let long_preview = "x".repeat(MAX_SEARCH_ROW_BYTES + 400);
        let mut entry = json!({
            "role": "search",
            "conversation_id": "conv",
            "content": format!(
                "/repo/a.rs:10:{}\n/repo/b.rs:20:{}",
                long_preview,
                "b".repeat(MAX_CARD_OUTPUT_BYTES)
            ),
        });
        sanitize_transcript_card_entry(&mut entry);
        let content = entry["content"].as_str().unwrap();
        assert!(content.len() <= MAX_CARD_OUTPUT_BYTES);
        assert!(content.lines().next().unwrap().len() <= MAX_SEARCH_ROW_BYTES);
        assert!(content.contains("/repo/a.rs:10:"));
        assert_eq!(entry["truncated"], true);
    }

    #[test]
    fn truncates_structured_view_lines() {
        let mut entry = json!({
            "role": "view",
            "conversation_id": "conv",
            "lines": [
                {"line_no": 1, "content": "a".repeat(MAX_CARD_OUTPUT_BYTES + 100)},
                {"line_no": 2, "content": "second"}
            ],
        });
        sanitize_transcript_card_entry(&mut entry);
        let lines = entry["lines"].as_array().unwrap();
        assert_eq!(lines.len(), 1);
        assert!(lines[0]["content"].as_str().unwrap().len() <= MAX_CARD_OUTPUT_BYTES);
        assert_eq!(entry["truncated"], true);
    }

    #[test]
    fn truncates_structured_tool_response() {
        let mut event = json!({
            "type": "tool_end",
            "conversation_id": "conv",
            "tool": "example",
            "request": {"path": "/repo/file"},
            "response": {"items": ["x".repeat(MAX_CARD_OUTPUT_BYTES + 100)]},
        });
        sanitize_live_card_event(&mut event);
        assert_eq!(event["request"]["path"], "/repo/file");
        assert_eq!(event["truncated"], true);
        let serialized = serde_json::to_string(&event["response"]).unwrap();
        assert!(serialized.len() <= MAX_CARD_OUTPUT_BYTES + 256);
    }
}
