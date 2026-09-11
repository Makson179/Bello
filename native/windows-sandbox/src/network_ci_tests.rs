//! CI-only packet attribution. This module never ships in the helper binary.
//! A failed socket call alone is not evidence of Bello's network isolation:
//! require a CLASSIFY_DROP naming one of this test lease's exact WFP IDs.

use anyhow::{anyhow, bail, ensure, Context, Result};
use std::ffi::c_void;
use std::mem;
use std::ptr;
use std::sync::{Condvar, Mutex};
use std::time::{Duration, Instant};
use windows_sys::Win32::Foundation::HANDLE;
use windows_sys::Win32::NetworkManagement::WindowsFilteringPlatform::*;

fn check(code: u32, operation: &str) -> Result<()> {
    ensure!(code == 0, "{operation}: Windows/WFP error 0x{code:08x}");
    Ok(())
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct DropEvidence {
    filter_id: u64,
    layer_id: u16,
    protocol: u8,
    ipv6: bool,
    remote_port: u16,
    loopback: bool,
}

struct Observed {
    ids: [u64; 4],
    drops: Mutex<Vec<DropEvidence>>,
    changed: Condvar,
}

unsafe extern "system" fn on_event(context: *mut c_void, event: *const FWPM_NET_EVENT2) {
    if context.is_null() || event.is_null() {
        return;
    }
    let state = &*(context as *const Observed);
    let event = &*event;
    if event.r#type != FWPM_NET_EVENT_TYPE_CLASSIFY_DROP {
        return;
    }
    let drop = event.Anonymous.classifyDrop;
    if drop.is_null() || !state.ids.contains(&(*drop).filterId) {
        return; // Do not retain any other application's network events.
    }
    let required = FWPM_NET_EVENT_FLAG_IP_PROTOCOL_SET
        | FWPM_NET_EVENT_FLAG_IP_VERSION_SET
        | FWPM_NET_EVENT_FLAG_REMOTE_PORT_SET;
    if event.header.flags & required != required
        || ![FWP_IP_VERSION_V4, FWP_IP_VERSION_V6].contains(&event.header.ipVersion)
    {
        return; // Missing metadata cannot establish this finite packet case.
    }
    let evidence = DropEvidence {
        filter_id: (*drop).filterId,
        layer_id: (*drop).layerId,
        protocol: event.header.ipProtocol,
        ipv6: event.header.ipVersion == FWP_IP_VERSION_V6,
        remote_port: event.header.remotePort,
        loopback: (*drop).isLoopback != 0,
    };
    // No panics may unwind through the Windows callback. A poisoned/full buffer
    // makes the subsequent explicit assertion fail, never count as a drop.
    if let Ok(mut drops) = state.drops.lock() {
        if drops.len() < 128 {
            drops.push(evidence);
            state.changed.notify_all();
        }
    }
}

struct DropObserver {
    engine: HANDLE,
    subscription: HANDLE,
    original_collection: Option<u32>,
    state: Option<Box<Observed>>,
}

impl DropObserver {
    fn start(ids: [u64; 4]) -> Result<Self> {
        ensure!(ids.iter().all(|id| *id != 0), "zero WFP filter ID");
        let mut observer = Self {
            engine: 0,
            subscription: 0,
            original_collection: None,
            state: Some(Box::new(Observed {
                ids,
                drops: Mutex::new(Vec::new()),
                changed: Condvar::new(),
            })),
        };
        check(
            unsafe {
                FwpmEngineOpen0(
                    ptr::null(),
                    10,
                    ptr::null(),
                    ptr::null(),
                    &mut observer.engine,
                )
            },
            "open CI WFP observer",
        )?;
        let original = observer.read_collection()?;
        // This administrator-only CI fixture is the sole collection-setting
        // writer in its job. Production never changes this machine setting.
        // Record before writing so even a failed readback follows restoration.
        observer.original_collection = Some(original);
        if original == 0 {
            observer.set_collection(1)?;
        }
        let subscription: FWPM_NET_EVENT_SUBSCRIPTION0 = unsafe { mem::zeroed() };
        let context = observer.state.as_deref().unwrap() as *const Observed as *const c_void;
        check(
            unsafe {
                FwpmNetEventSubscribe1(
                    observer.engine,
                    &subscription,
                    Some(on_event),
                    context,
                    &mut observer.subscription,
                )
            },
            "subscribe to CI lease drop events",
        )?;
        Ok(observer)
    }

    fn read_collection(&self) -> Result<u32> {
        let mut option = ptr::null_mut();
        check(
            unsafe {
                FwpmEngineGetOption0(self.engine, FWPM_ENGINE_COLLECT_NET_EVENTS, &mut option)
            },
            "read WFP event collection setting",
        )?;
        let value = if !option.is_null() && unsafe { (*option).r#type == FWP_UINT32 } {
            Some(unsafe { (*option).Anonymous.uint32 })
        } else {
            None
        };
        unsafe { FwpmFreeMemory0(&mut option as *mut _ as *mut *mut c_void) };
        match value {
            Some(value @ (0 | 1)) => Ok(value),
            _ => bail!("unexpected WFP collection option type/value: {value:?}"),
        }
    }

    fn set_collection(&self, enabled: u32) -> Result<()> {
        let mut value: FWP_VALUE0 = unsafe { mem::zeroed() };
        value.r#type = FWP_UINT32;
        value.Anonymous.uint32 = enabled;
        check(
            unsafe { FwpmEngineSetOption0(self.engine, FWPM_ENGINE_COLLECT_NET_EVENTS, &value) },
            "set temporary CI WFP collection option",
        )?;
        ensure!(
            self.read_collection()? == enabled,
            "WFP collection option readback mismatch"
        );
        Ok(())
    }

    fn require_drop(&self, protocol: u8, ipv6: bool, remote_port: u16) -> Result<DropEvidence> {
        let state = self.state.as_deref().unwrap();
        let deadline = Instant::now() + Duration::from_secs(5);
        let mut drops = state
            .drops
            .lock()
            .map_err(|_| anyhow!("WFP observer mutex poisoned"))?;
        loop {
            if let Some(drop) = drops.iter().find(|drop| {
                drop.protocol == protocol && drop.ipv6 == ipv6 && drop.remote_port == remote_port
            }) {
                return Ok(drop.clone());
            }
            let Some(remaining) = deadline.checked_duration_since(Instant::now()) else {
                bail!("no exact Bello WFP drop for protocol={protocol}, ipv6={ipv6}, port={remote_port}; observed={drops:?}");
            };
            drops = state
                .changed
                .wait_timeout(drops, remaining)
                .map_err(|_| anyhow!("WFP observer mutex poisoned"))?
                .0;
        }
    }

    fn finish(mut self) -> Result<()> {
        self.close()
    }

    fn close(&mut self) -> Result<()> {
        if self.subscription != 0 {
            // Microsoft guarantees this waits for in-flight callbacks. Do not
            // hold state.drops while unsubscribing, and keep the Box alive.
            check(
                unsafe { FwpmNetEventUnsubscribe0(self.engine, self.subscription) },
                "unsubscribe CI observer",
            )?;
            self.subscription = 0;
        }
        if self.engine != 0 {
            if self.original_collection == Some(0) {
                // Only undo the 0 -> 1 change owned by this fixture. If it is
                // already zero, leave it; unknown types/values are errors and
                // must never be overwritten by an assumed baseline.
                if self.read_collection()? == 1 {
                    self.set_collection(0)?;
                }
            }
            self.original_collection = None;
            check(
                unsafe { FwpmEngineClose0(self.engine) },
                "close CI WFP observer",
            )?;
            self.engine = 0;
        }
        Ok(())
    }
}

impl Drop for DropObserver {
    fn drop(&mut self) {
        if let Err(error) = self.close() {
            eprintln!("CI WFP observer cleanup failed: {error:#}");
            // Never free callback context if cancellation cannot be confirmed.
            if let Some(state) = self.state.take() {
                Box::leak(state);
            }
        }
    }
}

#[test]
fn drop_attribution_rejects_foreign_filter_and_missing_fields() {
    let state = Observed {
        ids: [1, 2, 3, 4],
        drops: Mutex::new(Vec::new()),
        changed: Condvar::new(),
    };
    let mut event: FWPM_NET_EVENT2 = unsafe { mem::zeroed() };
    let mut drop: FWPM_NET_EVENT_CLASSIFY_DROP2 = unsafe { mem::zeroed() };
    drop.filterId = 99;
    drop.layerId = 42;
    drop.isLoopback = 1;
    event.r#type = FWPM_NET_EVENT_TYPE_CLASSIFY_DROP;
    event.Anonymous.classifyDrop = &mut drop;
    event.header.flags = FWPM_NET_EVENT_FLAG_IP_PROTOCOL_SET
        | FWPM_NET_EVENT_FLAG_IP_VERSION_SET
        | FWPM_NET_EVENT_FLAG_REMOTE_PORT_SET;
    event.header.ipProtocol = 17;
    event.header.ipVersion = FWP_IP_VERSION_V6;
    event.header.remotePort = 55555;
    let context = &state as *const Observed as *mut c_void;
    unsafe { on_event(context, &event) };
    assert!(state.drops.lock().unwrap().is_empty());
    unsafe { (*event.Anonymous.classifyDrop).filterId = 2 };
    event.header.flags = 0;
    unsafe { on_event(context, &event) };
    assert!(state.drops.lock().unwrap().is_empty());
    event.header.flags = FWPM_NET_EVENT_FLAG_IP_PROTOCOL_SET
        | FWPM_NET_EVENT_FLAG_IP_VERSION_SET
        | FWPM_NET_EVENT_FLAG_REMOTE_PORT_SET;
    unsafe { on_event(context, &event) };
    assert_eq!(
        *state.drops.lock().unwrap(),
        [DropEvidence {
            filter_id: 2,
            layer_id: 42,
            protocol: 17,
            ipv6: true,
            remote_port: 55555,
            loopback: true
        }]
    );
}

use crate::identity::{
    create_profile, delete_profile, profile_local_app_data, random_profile_name, sid_string,
    CapabilitySids,
};
use crate::network_protocol::{LeaseRecord, NETWORK_POLICY_VERSION};
use crate::process::{clean_environment, run_child, Job};
use crate::protocol::SandboxMode;
use crate::winutil::{open_path, Handle};
use crate::{acl, offline_network};
use serde::{Deserialize, Serialize};
use std::fs;
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream, UdpSocket};
use std::path::Path;
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc,
};
use windows_sys::Win32::Foundation::{FILETIME, PSID};
use windows_sys::Win32::NetworkManagement::WindowsFirewall::{
    NetworkIsolationGetAppContainerConfig, NetworkIsolationSetAppContainerConfig,
};
use windows_sys::Win32::Security::{
    CreateRestrictedToken, EqualSid, ImpersonateLoggedOnUser, RevertToSelf, DISABLE_MAX_PRIVILEGE,
    SID_AND_ATTRIBUTES, TOKEN_DUPLICATE, TOKEN_QUERY,
};
use windows_sys::Win32::System::Memory::{GetProcessHeap, HeapFree};
use windows_sys::Win32::System::Threading::{GetCurrentProcess, GetProcessTimes, OpenProcessToken};

struct LoopbackList {
    entries: *mut SID_AND_ATTRIBUTES,
    count: u32,
}
impl LoopbackList {
    fn read() -> Result<Self> {
        let mut list = Self {
            entries: ptr::null_mut(),
            count: 0,
        };
        check(
            unsafe { NetworkIsolationGetAppContainerConfig(&mut list.count, &mut list.entries) },
            "read loopback exemptions",
        )?;
        ensure!(
            list.count <= 4096 && (list.count == 0 || !list.entries.is_null()),
            "invalid loopback exemption list"
        );
        Ok(list)
    }
    fn slice(&self) -> &[SID_AND_ATTRIBUTES] {
        if self.count == 0 {
            &[]
        } else {
            unsafe { std::slice::from_raw_parts(self.entries, self.count as usize) }
        }
    }
}
impl Drop for LoopbackList {
    fn drop(&mut self) {
        unsafe {
            let heap = GetProcessHeap();
            for entry in self.slice() {
                HeapFree(heap, 0, entry.Sid);
            }
            if !self.entries.is_null() {
                HeapFree(heap, 0, self.entries as *const c_void);
            }
        }
    }
}

fn set_test_loopback(sid: PSID, enabled: bool) -> Result<()> {
    // Debug exception for this freshly generated fixture SID only. Preserve
    // every other entry and reread before removal rather than restoring a stale
    // global snapshot. Production never installs this exception.
    let list = LoopbackList::read()?;
    let foreign = list
        .slice()
        .iter()
        .filter(|entry| unsafe { EqualSid(entry.Sid, sid) == 0 })
        .copied()
        .collect::<Vec<_>>();
    let matching = list.slice().len() - foreign.len();
    ensure!(matching <= 1, "duplicate fixture loopback SID");
    let before = foreign
        .iter()
        .map(|entry| Ok((sid_string(entry.Sid)?, entry.Attributes)))
        .collect::<Result<Vec<_>>>()?;
    let mut entries = foreign;
    if enabled {
        entries.push(SID_AND_ATTRIBUTES {
            Sid: sid,
            Attributes: 0,
        });
    }
    check(
        unsafe { NetworkIsolationSetAppContainerConfig(entries.len() as u32, entries.as_ptr()) },
        "set exact CI loopback exemption",
    )?;
    let verified = LoopbackList::read()?;
    let after = verified
        .slice()
        .iter()
        .filter(|entry| unsafe { EqualSid(entry.Sid, sid) == 0 })
        .map(|entry| Ok((sid_string(entry.Sid)?, entry.Attributes)))
        .collect::<Result<Vec<_>>>()?;
    ensure!(
        before == after,
        "CI loopback setup changed another package exception"
    );
    ensure!(
        verified
            .slice()
            .iter()
            .filter(|entry| unsafe { EqualSid(entry.Sid, sid) != 0 })
            .count()
            == usize::from(enabled),
        "CI loopback exception readback mismatch"
    );
    Ok(())
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct PacketCase {
    address: SocketAddr,
    udp: bool,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
struct PacketConfig {
    nonce: String,
    cases: Vec<PacketCase>,
}
#[derive(Debug, Serialize, Deserialize)]
struct PacketResult {
    case: PacketCase,
    echoed: bool,
    error: Option<String>,
}
#[derive(Debug, Serialize, Deserialize)]
struct ClientReport {
    direct: Vec<PacketResult>,
    restricted: Option<Vec<PacketResult>>,
    restricted_error: Option<String>,
}

fn exchange(case: &PacketCase, nonce: &[u8]) -> std::io::Result<()> {
    let mut reply = vec![0_u8; nonce.len()];
    if case.udp {
        let bind = if case.address.is_ipv6() {
            "[::1]:0"
        } else {
            "127.0.0.1:0"
        };
        let socket = UdpSocket::bind(bind)?;
        socket.set_read_timeout(Some(Duration::from_secs(2)))?;
        socket.connect(case.address)?;
        socket.send(nonce)?;
        let count = socket.recv(&mut reply)?;
        if count != nonce.len() {
            return Err(std::io::Error::other("short UDP nonce reply"));
        }
    } else {
        let mut socket = TcpStream::connect_timeout(&case.address, Duration::from_secs(2))?;
        socket.set_read_timeout(Some(Duration::from_secs(2)))?;
        socket.set_write_timeout(Some(Duration::from_secs(2)))?;
        socket.write_all(nonce)?;
        socket.read_exact(&mut reply)?;
    }
    if reply != nonce {
        return Err(std::io::Error::other("wrong echo nonce"));
    }
    Ok(())
}

fn packets(config: &PacketConfig) -> Vec<PacketResult> {
    config
        .cases
        .iter()
        .map(|case| {
            let result = exchange(case, config.nonce.as_bytes());
            PacketResult {
                case: case.clone(),
                echoed: result.is_ok(),
                error: result
                    .err()
                    .map(|error| format!("{error} (Win32 {:?})", error.raw_os_error())),
            }
        })
        .collect()
}

fn restricted_packets(config: &PacketConfig) -> Result<Vec<PacketResult>> {
    let mut raw = 0;
    ensure!(
        unsafe { OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY | TOKEN_DUPLICATE, &mut raw) }
            != 0,
        "OpenProcessToken failed: {}",
        std::io::Error::last_os_error()
    );
    let original = Handle::new(raw, "packet original token")?;
    let mut raw_restricted = 0;
    ensure!(
        unsafe {
            CreateRestrictedToken(
                original.raw(),
                DISABLE_MAX_PRIVILEGE,
                0,
                ptr::null(),
                0,
                ptr::null(),
                0,
                ptr::null(),
                &mut raw_restricted,
            )
        } != 0,
        "CreateRestrictedToken failed: {}",
        std::io::Error::last_os_error()
    );
    let restricted = Handle::new(raw_restricted, "packet restricted token")?;
    ensure!(
        unsafe { ImpersonateLoggedOnUser(restricted.raw()) } != 0,
        "ImpersonateLoggedOnUser failed: {}",
        std::io::Error::last_os_error()
    );
    struct Revert(bool);
    impl Drop for Revert {
        fn drop(&mut self) {
            if self.0 && unsafe { RevertToSelf() } == 0 {
                std::process::abort();
            }
        }
    }
    let mut revert = Revert(true);
    let results = packets(config);
    assert_ne!(
        unsafe { RevertToSelf() },
        0,
        "RevertToSelf failed: {}",
        std::io::Error::last_os_error()
    );
    revert.0 = false;
    Ok(results)
}

#[test]
fn ci_packet_client() -> Result<()> {
    // This test is selected explicitly in a copied executable inside the real
    // LPAC. An ordinary cargo run has no private fixture marker and does no I/O.
    let config_path = Path::new("bello-network-packet-fixture.json");
    if !config_path.exists() {
        return Ok(());
    }
    let config: PacketConfig = serde_json::from_slice(&fs::read(config_path)?)?;
    ensure!(
        config.cases.len() == 4 && config.nonce.len() == 32,
        "invalid packet fixture"
    );
    ensure!(
        config
            .cases
            .iter()
            .all(|case| case.address.ip().is_loopback()),
        "fixture only contacts its local nonce echo listeners"
    );
    let restricted = restricted_packets(&config);
    let (restricted, restricted_error) = match restricted {
        Ok(value) => (Some(value), None),
        Err(error) => (None, Some(format!("{error:#}"))),
    };
    let report = ClientReport {
        direct: packets(&config),
        restricted,
        restricted_error,
    };
    let descendant = std::env::var_os("BELLO_CI_NETWORK_DESCENDANT").is_some();
    let output = if descendant {
        "packet-descendant.json"
    } else {
        "packet-parent.json"
    };
    fs::write(output, serde_json::to_vec(&report)?)?;
    if !descendant {
        let status = std::process::Command::new(std::env::current_exe()?)
            .args([
                "--exact",
                "network_ci_tests::ci_packet_client",
                "--nocapture",
            ])
            .env("BELLO_CI_NETWORK_DESCENDANT", "1")
            .status()?;
        ensure!(
            status.success(),
            "ordinary LPAC descendant failed: {status}"
        );
    }
    Ok(())
}

struct EchoPeers {
    cases: Vec<PacketCase>,
    stop: Arc<AtomicBool>,
    threads: Vec<std::thread::JoinHandle<()>>,
}
impl EchoPeers {
    fn start() -> Result<Self> {
        let mut peers = Self {
            cases: Vec::new(),
            stop: Arc::new(AtomicBool::new(false)),
            threads: Vec::new(),
        };
        for address in ["127.0.0.1:0", "[::1]:0"] {
            let tcp = TcpListener::bind(address)?;
            tcp.set_nonblocking(true)?;
            peers.cases.push(PacketCase {
                address: tcp.local_addr()?,
                udp: false,
            });
            let stop = peers.stop.clone();
            peers.threads.push(std::thread::spawn(move || {
                while !stop.load(Ordering::Acquire) {
                    if let Ok((mut stream, _)) = tcp.accept() {
                        let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
                        let mut nonce = [0_u8; 32];
                        if stream.read_exact(&mut nonce).is_ok() {
                            let _ = stream.write_all(&nonce);
                        }
                    } else {
                        std::thread::sleep(Duration::from_millis(10));
                    }
                }
            }));
            let udp = UdpSocket::bind(address)?;
            udp.set_nonblocking(true)?;
            peers.cases.push(PacketCase {
                address: udp.local_addr()?,
                udp: true,
            });
            let stop = peers.stop.clone();
            peers.threads.push(std::thread::spawn(move || {
                let mut nonce = [0_u8; 32];
                while !stop.load(Ordering::Acquire) {
                    if let Ok((32, address)) = udp.recv_from(&mut nonce) {
                        let _ = udp.send_to(&nonce, address);
                    } else {
                        std::thread::sleep(Duration::from_millis(10));
                    }
                }
            }));
        }
        Ok(peers)
    }
}
impl Drop for EchoPeers {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Release);
        for thread in self.threads.drain(..) {
            let _ = thread.join();
        }
    }
}

fn process_creation() -> Result<u64> {
    let mut times: [FILETIME; 4] = unsafe { mem::zeroed() };
    let pointer = times.as_mut_ptr();
    ensure!(
        unsafe {
            GetProcessTimes(
                GetCurrentProcess(),
                pointer,
                pointer.add(1),
                pointer.add(2),
                pointer.add(3),
            )
        } != 0,
        "GetProcessTimes failed"
    );
    Ok((u64::from(times[0].dwHighDateTime) << 32) | u64::from(times[0].dwLowDateTime))
}

fn launch_packet_client(
    root: &Path,
    sid: PSID,
    capabilities: &mut CapabilitySids,
    expected_echo: bool,
) -> Result<Vec<PacketResult>> {
    for report in ["packet-parent.json", "packet-descendant.json"] {
        let path = root.join(report);
        if path.exists() {
            fs::remove_file(path)?;
        }
    }
    let job = Job::create()?;
    let mut environment = clean_environment(&profile_local_app_data(sid)?, root, &[])?;
    let cancelled = Arc::new(AtomicBool::new(false));
    let (finished, waiting) = std::sync::mpsc::channel();
    let watchdog_job = job.clone();
    let watchdog = std::thread::spawn(move || {
        if waiting.recv_timeout(Duration::from_secs(60)).is_err() {
            watchdog_job.terminate(124)?;
            bail!("packet child exceeded its 60-second CI bound");
        }
        Ok::<_, anyhow::Error>(())
    });
    let result = run_child(
        "packet-client.exe --exact network_ci_tests::ci_packet_client --nocapture",
        root,
        sid,
        capabilities,
        &mut environment,
        &job,
        &cancelled,
    );
    let _ = finished.send(());
    let watchdog_result = watchdog
        .join()
        .map_err(|_| anyhow!("packet watchdog panicked"))?;
    job.terminate(125)?;
    job.ensure_empty()?;
    watchdog_result?;
    ensure!(result? == 0, "native LPAC packet fixture did not complete");
    let mut results = Vec::new();
    for name in ["packet-parent.json", "packet-descendant.json"] {
        let report: ClientReport = serde_json::from_slice(&fs::read(root.join(name))?)?;
        ensure!(report.direct.len() == 4, "packet client omitted cases");
        if let Some(error) = &report.restricted_error {
            eprintln!("{name} restricted-token API rejected: {error}");
        }
        for case in report
            .direct
            .iter()
            .chain(report.restricted.iter().flatten())
        {
            ensure!(
                case.echoed == expected_echo,
                "{name} expected_echo={expected_echo}, result={case:?}"
            );
        }
        results.extend(report.direct);
    }
    Ok(results)
}

#[test]
fn actual_lpac_tcp_udp_v4_v6_is_blocked_by_exact_lease_and_restored() -> Result<()> {
    // Exercise the core independently of broker authentication. Separate Pi and
    // non-admin tests exercise real register/release through the service pipe.
    offline_network::validate_layout()?;
    let profile = random_profile_name()?;
    let mut capabilities = CapabilitySids::for_network(true)?;
    let supplied =
        std::env::temp_dir().join(format!("bello-packet-ci-{}", offline_network::new_id()?));
    fs::create_dir(&supplied)?;
    let root = fs::canonicalize(&supplied)?;
    let sid = match create_profile(&profile, &capabilities) {
        Ok(sid) => sid,
        Err(error) => {
            fs::remove_dir(&root)?;
            return Err(error);
        }
    };
    let mut lease: Option<LeaseRecord> = None;
    let mut exemption_installed = false;
    let outcome = (|| -> Result<()> {
        let peers = EchoPeers::start()?;
        let config = PacketConfig {
            nonce: offline_network::new_id()?.replace('-', ""),
            cases: peers.cases.clone(),
        };
        ensure!(config.nonce.len() == 32, "unexpected nonce format");
        fs::copy(std::env::current_exe()?, root.join("packet-client.exe"))?;
        fs::write(
            root.join("bello-network-packet-fixture.json"),
            serde_json::to_vec(&config)?,
        )?;
        for path in [
            &root,
            &root.join("packet-client.exe"),
            &root.join("bello-network-packet-fixture.json"),
        ] {
            acl::grant(&open_path(path, true)?, sid.0, SandboxMode::WorkspaceWrite)?;
        }
        exemption_installed = true;
        set_test_loopback(sid.0, true)?;
        launch_packet_client(&root, sid.0, &mut capabilities, true)
            .context("online positive control before blocking")?;
        let record = LeaseRecord {
            policy_version: NETWORK_POLICY_VERSION,
            profile_name: profile.clone(),
            package_sid: sid_string(sid.0)?,
            owner_sid: acl::current_account_sid_string()?,
            caller_pid: std::process::id(),
            caller_creation: process_creation()?,
            boot_id: offline_network::current_boot_id()?,
            lease_id: offline_network::new_id()?,
            filter_keys: [
                offline_network::new_id()?,
                offline_network::new_id()?,
                offline_network::new_id()?,
                offline_network::new_id()?,
            ],
        };
        let installed = offline_network::install_lease(record.clone())?;
        lease = Some(record);
        let observer = DropObserver::start(installed.filter_ids)?;
        let attempts = launch_packet_client(&root, sid.0, &mut capabilities, false)
            .context("offline LPAC and ordinary-child packet attempts")?;
        for result in attempts {
            let evidence = observer.require_drop(
                if result.case.udp { 17 } else { 6 },
                result.case.address.is_ipv6(),
                result.case.address.port(),
            )?;
            ensure!(
                evidence.loopback,
                "local packet drop was not attributed to loopback"
            );
            eprintln!("Bello packet proof: {evidence:?}");
        }
        observer.finish()?;
        offline_network::remove_lease(lease.as_ref().unwrap())?;
        lease = None;
        launch_packet_client(&root, sid.0, &mut capabilities, true)
            .context("online positive control after removing only this lease")?;
        Ok(())
    })();
    // Cleanup is deliberately independent of assertions. Never leave a WFP
    // filter or debug loopback exception behind after a failed positive control.
    let mut cleanup_errors = Vec::new();
    if let Some(record) = lease {
        if let Err(error) = offline_network::remove_lease(&record) {
            cleanup_errors.push(format!("lease: {error:#}"));
        }
    }
    if exemption_installed {
        if let Err(error) = set_test_loopback(sid.0, false) {
            cleanup_errors.push(format!("loopback: {error:#}"));
        }
    }
    if let Err(error) = fs::remove_dir_all(&root) {
        cleanup_errors.push(format!("fixture: {error}"));
    }
    if let Err(error) = delete_profile(&profile) {
        cleanup_errors.push(format!("profile: {error:#}"));
    }
    ensure!(
        cleanup_errors.is_empty(),
        "packet fixture cleanup failed: {cleanup_errors:?}; original={outcome:?}"
    );
    outcome
}
