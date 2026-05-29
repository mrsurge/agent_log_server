use anyhow::{Context, Result, anyhow, bail};
use git2::{Repository, Status, StatusOptions};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs, io,
    path::{Component, Path, PathBuf},
    process::Command,
};

#[derive(Debug)]
struct ProjectRepo {
    root: PathBuf,
}

#[derive(Debug)]
struct GitOutput {
    stdout: String,
    stderr: String,
}

pub fn stage_paths(start: &Path, raw_paths: &[String]) -> Result<Value> {
    let repo = discover_project_repo(start)?;
    let paths = validate_targets(raw_paths)?;
    let mut args = vec!["add", "-A"];
    if !paths.is_empty() {
        args.push("--");
    }
    run_git_pathspec(&repo.root, &args, &paths)?;
    Ok(json!({
        "ok": true,
        "root": path_to_string(&repo.root),
        "paths": paths,
        "transport": "rpc",
    }))
}

pub fn unstage_paths(start: &Path, raw_paths: &[String]) -> Result<Value> {
    let repo = discover_project_repo(start)?;
    let paths = if raw_paths.is_empty() {
        status_paths(&repo.root)?
    } else {
        validate_targets(raw_paths)?
    };
    if !paths.is_empty() {
        run_git_pathspec(&repo.root, &["reset", "--quiet", "--"], &paths)?;
    }
    Ok(json!({
        "ok": true,
        "root": path_to_string(&repo.root),
        "paths": paths,
        "transport": "rpc",
    }))
}

pub fn restore_paths(start: &Path, raw_paths: &[String]) -> Result<Value> {
    let repo = discover_project_repo(start)?;
    let paths = if raw_paths.is_empty() {
        status_paths(&repo.root)?
    } else {
        validate_targets(raw_paths)?
    };
    if paths.is_empty() {
        return Ok(json!({
            "ok": true,
            "root": path_to_string(&repo.root),
            "paths": Vec::<String>::new(),
            "restored": 0,
            "transport": "rpc",
        }));
    }

    let statuses = collect_statuses(&repo.root)?;
    let mut index_reset_paths = Vec::new();
    let mut tracked_restore_paths = Vec::new();
    let mut delete_paths = Vec::new();

    for path in &paths {
        let status = statuses.get(path).copied().unwrap_or_else(Status::empty);
        if status.is_empty() {
            continue;
        }
        if has_index_change(status) {
            index_reset_paths.push(path.clone());
        }
        if is_new_path(status) {
            delete_paths.push(path.clone());
        } else {
            tracked_restore_paths.push(path.clone());
        }
    }

    if !index_reset_paths.is_empty() {
        run_git_pathspec(&repo.root, &["reset", "--quiet", "--"], &index_reset_paths)?;
    }
    if !tracked_restore_paths.is_empty() {
        run_git_pathspec(
            &repo.root,
            &["restore", "--worktree", "--source=HEAD", "--"],
            &tracked_restore_paths,
        )?;
    }
    for path in &delete_paths {
        remove_repo_path(&repo.root, path)?;
    }

    Ok(json!({
        "ok": true,
        "root": path_to_string(&repo.root),
        "paths": paths,
        "restored": tracked_restore_paths.len() + delete_paths.len(),
        "transport": "rpc",
    }))
}

pub fn commit_staged(start: &Path, message: &str) -> Result<Value> {
    let trimmed = message.trim();
    if trimmed.is_empty() {
        bail!("Commit message is required");
    }
    let repo = discover_project_repo(start)?;
    let diff_check = run_git_status(&repo.root, &["diff", "--cached", "--quiet", "--exit-code"])?;
    if diff_check == 0 {
        bail!("Nothing is staged");
    }
    if diff_check != 1 {
        bail!("Unable to inspect staged changes");
    }

    let output = run_git(&repo.root, &["commit", "-m", trimmed])?;
    let head = run_git(&repo.root, &["rev-parse", "HEAD"])?;
    let hash = head.stdout.trim().to_owned();
    let short = hash.chars().take(12).collect::<String>();
    Ok(json!({
        "ok": true,
        "root": path_to_string(&repo.root),
        "commit": hash,
        "commit_short": short,
        "stdout": output.stdout,
        "stderr": output.stderr,
        "transport": "rpc",
    }))
}

fn discover_project_repo(start: &Path) -> Result<ProjectRepo> {
    let repo = Repository::discover(start)
        .with_context(|| format!("No git repository found from {}", start.display()))?;
    let root = repo
        .workdir()
        .map(Path::to_path_buf)
        .unwrap_or_else(|| repo.path().to_path_buf());
    Ok(ProjectRepo { root })
}

fn collect_statuses(root: &Path) -> Result<BTreeMap<String, Status>> {
    let repo = Repository::discover(root)?;
    let mut options = StatusOptions::new();
    options
        .include_untracked(true)
        .recurse_untracked_dirs(true)
        .renames_head_to_index(true)
        .renames_index_to_workdir(true);
    let statuses = repo.statuses(Some(&mut options))?;
    let mut out = BTreeMap::new();
    for entry in statuses.iter() {
        let Some(path) = entry.path() else {
            continue;
        };
        out.insert(path.to_owned(), entry.status());
    }
    Ok(out)
}

fn status_paths(root: &Path) -> Result<Vec<String>> {
    let mut paths = BTreeSet::new();
    for (path, _) in collect_statuses(root)? {
        paths.insert(path);
    }
    Ok(paths.into_iter().collect())
}

fn validate_targets(raw_paths: &[String]) -> Result<Vec<String>> {
    let mut paths = BTreeSet::new();
    for raw in raw_paths {
        let path = raw.trim();
        if path.is_empty() {
            continue;
        }
        validate_relative_path(path)?;
        paths.insert(path.to_owned());
    }
    Ok(paths.into_iter().collect())
}

fn validate_relative_path(path: &str) -> Result<()> {
    let path = Path::new(path);
    if path.is_absolute() {
        bail!("Project git targets must be repo-relative paths");
    }
    for component in path.components() {
        match component {
            Component::Normal(_) => {}
            Component::CurDir => {}
            _ => bail!("Project git target escapes the repository"),
        }
    }
    Ok(())
}

fn run_git_pathspec(root: &Path, args: &[&str], paths: &[String]) -> Result<GitOutput> {
    let mut command = Command::new("git");
    command.arg("-C").arg(root);
    for arg in args {
        command.arg(arg);
    }
    for path in paths {
        command.arg(path);
    }
    output_to_result(command.output(), args)
}

fn run_git(root: &Path, args: &[&str]) -> Result<GitOutput> {
    let mut command = Command::new("git");
    command.arg("-C").arg(root);
    for arg in args {
        command.arg(arg);
    }
    output_to_result(command.output(), args)
}

fn run_git_status(root: &Path, args: &[&str]) -> Result<i32> {
    let mut command = Command::new("git");
    command.arg("-C").arg(root);
    for arg in args {
        command.arg(arg);
    }
    let output = command.output()?;
    Ok(output.status.code().unwrap_or(2))
}

fn output_to_result(output: io::Result<std::process::Output>, args: &[&str]) -> Result<GitOutput> {
    let output = output?;
    let stdout = String::from_utf8_lossy(&output.stdout).into_owned();
    let stderr = String::from_utf8_lossy(&output.stderr).into_owned();
    if !output.status.success() {
        let detail = stderr.trim().to_owned();
        return Err(anyhow!(
            "git {} failed{}{}",
            args.join(" "),
            if detail.is_empty() { "" } else { ": " },
            detail
        ));
    }
    Ok(GitOutput { stdout, stderr })
}

fn has_index_change(status: Status) -> bool {
    status.intersects(
        Status::INDEX_NEW
            | Status::INDEX_MODIFIED
            | Status::INDEX_DELETED
            | Status::INDEX_RENAMED
            | Status::INDEX_TYPECHANGE
            | Status::CONFLICTED,
    )
}

fn is_new_path(status: Status) -> bool {
    status.contains(Status::WT_NEW) || status.contains(Status::INDEX_NEW)
}

fn remove_repo_path(root: &Path, rel_path: &str) -> Result<()> {
    validate_relative_path(rel_path)?;
    let path = root.join(rel_path);
    match fs::symlink_metadata(&path) {
        Ok(metadata) if metadata.is_dir() => fs::remove_dir_all(&path)
            .with_context(|| format!("Failed to remove {}", path.display())),
        Ok(_) => {
            fs::remove_file(&path).with_context(|| format!("Failed to remove {}", path.display()))
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(error).with_context(|| format!("Failed to inspect {}", path.display())),
    }
}

fn path_to_string(path: &Path) -> String {
    path.to_string_lossy().into_owned()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn validate_targets_rejects_absolute_and_parent_paths() {
        assert!(validate_targets(&["src/lib.rs".to_owned()]).is_ok());
        let absolute = format!("{}absolute-file", std::path::MAIN_SEPARATOR);
        assert!(validate_targets(&[absolute]).is_err());
        assert!(validate_targets(&["../file".to_owned()]).is_err());
    }

    #[test]
    fn validate_targets_deduplicates_and_sorts_paths() {
        let paths = validate_targets(&["b.txt".to_owned(), "a.txt".to_owned(), "b.txt".to_owned()])
            .expect("targets should validate");
        assert_eq!(paths, vec!["a.txt", "b.txt"]);
    }
}
