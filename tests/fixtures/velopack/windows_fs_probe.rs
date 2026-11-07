//! Small Windows experiments using the same std::fs::rename as the updater.
//! No shell move, copy/delete fallback, application dependencies, or certification.
use std::os::windows::fs::OpenOptionsExt;
use std::{
    env, fs, io,
    path::Path,
    process::{self, Command},
    thread,
    time::{Duration, Instant},
};

fn exclusive(path: &Path, share: u32) -> io::Result<fs::File> {
    fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .share_mode(share)
        .open(path)
}

fn observe(name: &str, result: &io::Result<()>) {
    println!(
        "{{\"case\":\"{}\",\"success\":{},\"windows_error\":{}}}",
        name,
        result.is_ok(),
        result
            .as_ref()
            .err()
            .and_then(|e| e.raw_os_error())
            .map(|e| e.to_string())
            .unwrap_or_else(|| "null".into())
    );
}

fn wait_file(path: &Path, child: &mut process::Child) -> io::Result<()> {
    let deadline = Instant::now() + Duration::from_secs(10);
    while !path.exists() {
        assert!(child.try_wait()?.is_none(), "child exited before readiness");
        assert!(Instant::now() < deadline, "child readiness timeout");
        thread::sleep(Duration::from_millis(20));
    }
    Ok(())
}

fn main() -> io::Result<()> {
    let args: Vec<_> = env::args_os().collect();
    if args.get(1).and_then(|s| s.to_str()) == Some("lock") {
        process::exit(if exclusive(Path::new(&args[2]), 0).is_err() {
            42
        } else {
            0
        });
    }
    if args.get(1).and_then(|s| s.to_str()) == Some("cwd") {
        env::set_current_dir(&args[2])?;
        fs::write(&args[3], b"ready")?;
        let deadline = Instant::now() + Duration::from_secs(15);
        while !Path::new(&args[4]).exists() {
            assert!(Instant::now() < deadline, "child release timeout");
            thread::sleep(Duration::from_millis(20));
        }
        return Ok(());
    }
    if args.get(1).and_then(|s| s.to_str()) == Some("self-rename") {
        let result = fs::rename(&args[2], &args[3]);
        observe("running_image_external_cwd", &result);
        // Report the observation without assuming loaded-image rename semantics.
        return Ok(());
    }
    let work = Path::new(args.get(1).expect("supply an isolated output directory"));
    fs::create_dir_all(work)?;
    let work = fs::canonicalize(work)?;
    let me = env::current_exe()?;
    env::set_current_dir(&work)?;
    for share in [0, 4, 7] {
        let root = work.join(format!("inner-share-{share}"));
        let retired = root.with_extension("retired");
        fs::create_dir_all(root.join("packages"))?;
        fs::write(root.join("payload"), b"preserve")?;
        let held = exclusive(&root.join("packages/.velopack_lock"), share)?;
        let result = fs::rename(&root, &retired);
        observe(&format!("inner_lock_share_{share}"), &result);
        assert!(
            result.is_err(),
            "open descendant unexpectedly allowed rename"
        );
        assert!(
            root.exists() && !retired.exists(),
            "failed rename partially changed paths"
        );
        drop(held);
        let result = fs::rename(&root, &retired);
        observe(&format!("closed_inner_lock_share_{share}"), &result);
        result?;
        assert_eq!(fs::read(retired.join("payload"))?, b"preserve");
    }
    let root = work.join("cwd-root");
    fs::create_dir(&root)?;
    let ready = work.join("cwd.ready");
    let stop = work.join("cwd.stop");
    let mut child = Command::new(&me)
        .arg("cwd")
        .arg(&root)
        .arg(&ready)
        .arg(&stop)
        .spawn()?;
    wait_file(&ready, &mut child)?;
    let result = fs::rename(&root, root.with_extension("retired"));
    observe("current_directory_inside", &result);
    fs::write(stop, b"stop")?;
    assert!(child.wait()?.success());
    assert!(
        result.is_err(),
        "live current directory unexpectedly allowed rename"
    );
    fs::rename(&root, root.with_extension("retired"))?;

    let root = work.join("installation é %literal%! & with spaces");
    let retired = root.with_extension("retired");
    let lock = work.join(".installation.tempo-owner.lock");
    fs::create_dir(&root)?;
    fs::write(root.join("payload"), b"original")?;
    let held = exclusive(&lock, 0)?;
    let contender = || Command::new(&me).arg("lock").arg(&lock).status();
    assert_eq!(contender()?.code(), Some(42));
    let owner = root.join("Update.exe");
    fs::copy(&me, &owner)?;
    assert!(Command::new(owner)
        .arg("self-rename")
        .arg(&root)
        .arg(&retired)
        .current_dir(&work)
        .status()?
        .success());
    // The outside-lock protocol must work even if a running image prevents retirement.
    if root.exists() {
        fs::rename(&root, &retired)?; // original child has now exited
        println!("{{\"case\":\"retirement_after_image_exit\",\"success\":true}}");
    }
    assert_eq!(contender()?.code(), Some(42));
    fs::create_dir(&root)?;
    fs::write(root.join("payload"), b"replacement")?;
    fs::remove_dir_all(&retired)?;
    assert_eq!(fs::read(root.join("payload"))?, b"replacement");
    assert_eq!(contender()?.code(), Some(42));
    drop(held);
    assert_eq!(contender()?.code(), Some(0));
    println!("{{\"case\":\"external_lock_reuse_and_cleanup\",\"success\":true}}");
    Ok(())
}
