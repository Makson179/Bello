//! Test-only DNS broker regression. No adapter/resolver settings are changed.
//! Direct socket denial does not prove that DNS Client cannot send on a child's
//! behalf. Observe only fresh fixture names at an explicit loopback responder.

use crate::identity::{
    create_profile, delete_profile, profile_local_app_data, random_profile_name, sid_string,
    CapabilitySids,
};
use crate::network_protocol::{LeaseRecord, NETWORK_POLICY_VERSION};
use crate::process::{clean_environment, run_child, Job};
use crate::protocol::SandboxMode;
use crate::winutil::open_path;
use crate::{acl, offline_network};
use anyhow::{anyhow, bail, ensure, Context, Result};
use serde::{Deserialize, Serialize};
use std::cell::Cell;
use std::collections::BTreeSet;
use std::fs;
use std::mem;
use std::net::{Ipv4Addr, UdpSocket};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::ptr;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use windows_sys::Win32::Foundation::{FILETIME, PSID};
use windows_sys::Win32::NetworkManagement::Dns::*;
use windows_sys::Win32::Networking::WinSock::AF_INET;
use windows_sys::Win32::System::SystemInformation::GetSystemDirectoryW;
use windows_sys::Win32::System::Threading::{GetCurrentProcess, GetProcessTimes};

const CONFIG: &str = "bello-local-dns-fixture.json";
const REPORT: &str = "bello-local-dns-result.json";
const CLIENT_TEST: &str = "network_dns_tests::ci_dns_client";
const CHILD_BOUND: Duration = Duration::from_secs(30);

#[derive(Deserialize, Serialize)]
struct QueryConfig {
    // No arbitrary address or hostname is accepted by this test client.
    nonce: String,
    port: u16,
}

impl QueryConfig {
    fn name(&self) -> Result<String> {
        ensure!(
            self.nonce.len() == 32
                && self.nonce.bytes().all(|byte| byte.is_ascii_hexdigit())
                && self.port != 0,
            "invalid local DNS fixture"
        );
        Ok(format!("bello-{}.invalid.", self.nonce))
    }
}

#[derive(Debug, Deserialize, Serialize)]
struct QueryResult {
    returned: i32,
    status: i32,
    records: bool,
}

fn local_dns_servers(port: u16) -> DNS_ADDR_ARRAY {
    let mut servers: DNS_ADDR_ARRAY = unsafe { mem::zeroed() };
    // Win32's DNS_ADDR_ARRAY specifies MaxCount in BYTES, not entries.
    // AddrCount holds the entry count; all reserved fields remain zero.
    // https://learn.microsoft.com/en-us/windows/win32/api/windnsdef/ns-windnsdef-dns_addr_array
    servers.MaxCount = mem::size_of::<DNS_ADDR_ARRAY>() as u32;
    servers.AddrCount = 1;
    servers.Family = AF_INET;
    // DNS_ADDR starts with sockaddr_in. Use bytes to avoid unaligned references
    // to this SDK's packed array. The host control verifies custom-port support.
    servers.AddrArray[0].MaxSa[..2].copy_from_slice(&AF_INET.to_ne_bytes());
    servers.AddrArray[0].MaxSa[2..4].copy_from_slice(&port.to_be_bytes());
    servers.AddrArray[0].MaxSa[4..8].copy_from_slice(&Ipv4Addr::LOCALHOST.octets());
    servers
}

#[test]
fn local_dns_server_array_matches_win32_layout() {
    let servers = local_dns_servers(32123);
    assert_eq!(
        { servers.MaxCount },
        mem::size_of::<DNS_ADDR_ARRAY>() as u32
    );
    assert_eq!({ servers.AddrCount }, 1);
    assert_eq!({ servers.Family }, AF_INET);
    assert_eq!(&servers.AddrArray[0].MaxSa[..2], &AF_INET.to_ne_bytes());
    assert_eq!(&servers.AddrArray[0].MaxSa[2..4], &32123_u16.to_be_bytes());
    assert_eq!(&servers.AddrArray[0].MaxSa[4..8], &[127, 0, 0, 1]);
    assert!(servers.AddrArray[0].MaxSa[8..]
        .iter()
        .all(|byte| *byte == 0));
    let reserved = unsafe { servers.AddrArray[0].Data.DnsAddrUserDword };
    assert_eq!(reserved, [0; 8]);
    assert_eq!({ servers.Tag }, 0);
    assert_eq!({ servers.WordReserved }, 0);
    assert_eq!({ servers.Flags }, 0);
    assert_eq!({ servers.MatchFlag }, 0);
    assert_eq!({ servers.Reserved1 }, 0);
    assert_eq!({ servers.Reserved2 }, 0);
}

#[test]
fn ci_dns_client() -> Result<()> {
    if !Path::new(CONFIG).exists() {
        return Ok(()); // Only the explicitly staged test executable does I/O.
    }
    let input = fs::read(CONFIG)?;
    ensure!(input.len() <= 256, "oversized DNS fixture config");
    let config: QueryConfig = serde_json::from_slice(&input)?;
    let name: Vec<u16> = config.name()?.encode_utf16().chain(Some(0)).collect();
    let mut servers = local_dns_servers(config.port);
    let mut request: DNS_QUERY_REQUEST = unsafe { mem::zeroed() };
    request.Version = DNS_QUERY_REQUEST_VERSION1;
    request.QueryName = name.as_ptr();
    request.QueryType = DNS_TYPE_A;
    request.QueryOptions = u64::from(
        DNS_QUERY_BYPASS_CACHE
            | DNS_QUERY_WIRE_ONLY
            | DNS_QUERY_NO_HOSTS_FILE
            | DNS_QUERY_NO_LOCAL_NAME
            | DNS_QUERY_NO_NETBT
            | DNS_QUERY_NO_MULTICAST
            | DNS_QUERY_TREAT_AS_FQDN,
    );
    request.pDnsServerList = &mut servers;
    let mut result: DNS_QUERY_RESULT = unsafe { mem::zeroed() };
    result.Version = DNS_QUERY_RESULTS_VERSION1;
    // Null callback means synchronous. The parent owns a bounded process/job
    // watchdog, so no callback can outlive these stack allocations.
    let returned = unsafe { DnsQueryEx(&request, &mut result, ptr::null_mut()) };
    let report = QueryResult {
        returned,
        status: result.QueryStatus,
        records: !result.pQueryRecords.is_null(),
    };
    if !result.pQueryRecords.is_null() {
        unsafe { DnsFree(result.pQueryRecords.cast(), DnsFreeRecordList) };
    }
    fs::write(REPORT, serde_json::to_vec(&report)?)?;
    Ok(())
}

// This responder deliberately implements only the fixture's exact A/IN
// question. It neither forwards nor resolves any name and retains no other
// machine/application DNS data. Compression, multiple questions and malformed
// packets are rejected instead of becoming a general DNS implementation.
fn response(packet: &[u8], allowed: &BTreeSet<String>) -> Option<(String, Vec<u8>)> {
    if packet.len() < 12 || packet.len() > 512 || packet[2] & 0xf8 != 0 {
        return None;
    }
    if packet[4..6] != [0, 1] || packet[6..10] != [0, 0, 0, 0] {
        return None;
    }
    let mut offset = 12;
    let mut labels = Vec::new();
    loop {
        let length = usize::from(*packet.get(offset)?);
        offset += 1;
        if length == 0 {
            break;
        }
        if length > 63 || labels.len() >= 2 {
            return None;
        }
        labels.push(std::str::from_utf8(packet.get(offset..offset + length)?).ok()?);
        offset += length;
    }
    let name = format!("{}.", labels.join(".")).to_ascii_lowercase();
    if !allowed.contains(&name) || packet.get(offset..offset + 4)? != [0, 1, 0, 1] {
        return None;
    }
    offset += 4;
    let mut reply = packet[..offset].to_vec();
    reply[2] = 0x81; // Response with original recursive request satisfied locally.
    reply[3] = 0x80;
    reply[6..8].copy_from_slice(&[0, 1]);
    reply[10..12].copy_from_slice(&[0, 0]); // Do not copy EDNS additional records.
    reply.extend_from_slice(&[0xc0, 0x0c, 0, 1, 0, 1, 0, 0, 0, 0, 0, 4, 127, 0, 0, 42]);
    Some((name, reply))
}

struct Responder {
    port: u16,
    observed: Arc<Mutex<BTreeSet<String>>>,
    stop: Arc<AtomicBool>,
    thread: Option<std::thread::JoinHandle<Result<()>>>,
}

impl Responder {
    fn start(allowed: BTreeSet<String>) -> Result<Self> {
        let socket = UdpSocket::bind((Ipv4Addr::LOCALHOST, 0))?;
        socket.set_read_timeout(Some(Duration::from_millis(100)))?;
        let port = socket.local_addr()?.port();
        let observed = Arc::new(Mutex::new(BTreeSet::new()));
        let stop = Arc::new(AtomicBool::new(false));
        let received = observed.clone();
        let stopped = stop.clone();
        let thread = std::thread::spawn(move || {
            let mut buffer = [0_u8; 513];
            while !stopped.load(Ordering::Acquire) {
                match socket.recv_from(&mut buffer) {
                    Ok((count, source)) if source.ip().is_loopback() => {
                        if let Some((name, reply)) = response(&buffer[..count], &allowed) {
                            received
                                .lock()
                                .map_err(|_| anyhow!("DNS observer poisoned"))?
                                .insert(name);
                            socket.send_to(&reply, source)?;
                        }
                    }
                    Ok(_) => {}
                    Err(error)
                        if matches!(
                            error.kind(),
                            std::io::ErrorKind::TimedOut | std::io::ErrorKind::WouldBlock
                        ) => {}
                    Err(error) => return Err(error.into()),
                }
            }
            Ok(())
        });
        Ok(Self {
            port,
            observed,
            stop,
            thread: Some(thread),
        })
    }

    fn saw(&self, name: &str) -> Result<bool> {
        Ok(self
            .observed
            .lock()
            .map_err(|_| anyhow!("DNS observer poisoned"))?
            .contains(name))
    }

    fn finish(&mut self) -> Result<()> {
        self.stop.store(true, Ordering::Release);
        if let Some(thread) = self.thread.take() {
            thread
                .join()
                .map_err(|_| anyhow!("DNS responder panicked"))??;
        }
        Ok(())
    }
}

impl Drop for Responder {
    fn drop(&mut self) {
        if let Err(error) = self.finish() {
            eprintln!("DNS responder cleanup failed: {error:#}");
        }
    }
}

fn bounded_host(command: &mut Command) -> Result<()> {
    let mut child = command.stdin(Stdio::null()).spawn()?;
    let deadline = Instant::now() + CHILD_BOUND;
    loop {
        match child.try_wait() {
            Ok(Some(status)) => {
                ensure!(status.success(), "DNS host fixture failed: {status}");
                return Ok(());
            }
            Ok(None) if Instant::now() < deadline => std::thread::sleep(Duration::from_millis(25)),
            outcome => {
                let _ = child.kill();
                let _ = child.wait();
                bail!("DNS host fixture exceeded bound or wait failed: {outcome:?}");
            }
        }
    }
}

fn no_nrpt_override() -> Result<()> {
    // DnsQueryEx custom servers can be overridden by NRPT. Before generating
    // any query, refuse a configured NRPT rather than risk contacting an
    // external policy-selected resolver. This reads only; no policy is changed.
    let mut buffer = [0_u16; 32768];
    let count = unsafe { GetSystemDirectoryW(buffer.as_mut_ptr(), buffer.len() as u32) } as usize;
    ensure!(
        count != 0 && count < buffer.len(),
        "cannot locate OS PowerShell for DNS preflight"
    );
    let executable = PathBuf::from(String::from_utf16(&buffer[..count])?)
        .join(r"WindowsPowerShell\v1.0\powershell.exe");
    bounded_host(Command::new(executable).args([
        "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
        "$ErrorActionPreference='Stop'; if (@(Get-DnsClientNrptPolicy -Effective).Count -ne 0) { throw 'Local DNS fixture refuses effective NRPT rules' }; if ((Get-Service Dnscache).Status -ne 'Running') { throw 'DNS Client service must be running for broker test' }",
    ]))
}

fn launch(
    root: &Path,
    sid: Option<PSID>,
    network: bool,
    safe_cleanup: &Cell<bool>,
) -> Result<QueryResult> {
    let output = root.join(REPORT);
    if output.exists() {
        fs::remove_file(&output)?;
    }
    if let Some(sid) = sid {
        let job = Job::create()?;
        let mut caps = CapabilitySids::for_network(network)?;
        let mut environment = clean_environment(&profile_local_app_data(sid)?, root, &[])?;
        let cancelled = Arc::new(AtomicBool::new(false));
        let (finished, waiting) = std::sync::mpsc::channel();
        let watched = job.clone();
        let watchdog = std::thread::spawn(move || -> Result<()> {
            if waiting.recv_timeout(CHILD_BOUND).is_err() {
                watched.terminate(124)?;
                bail!("DNS LPAC fixture exceeded its bound");
            }
            Ok(())
        });
        safe_cleanup.set(false);
        let outcome = run_child(
            &format!("dns-client.exe --exact {CLIENT_TEST} --nocapture"),
            root,
            sid,
            &mut caps,
            &mut environment,
            &job,
            &cancelled,
        );
        let _ = finished.send(());
        let watch_result = watchdog
            .join()
            .map_err(|_| anyhow!("DNS watchdog panicked"));
        // Complete cleanup even when launch or the watchdog failed.
        let termination = job.terminate(125);
        let empty = job.ensure_empty();
        if empty.is_ok() {
            safe_cleanup.set(true);
        }
        termination?;
        empty?;
        watch_result??;
        ensure!(
            outcome? == 0,
            "DNS LPAC client did not complete its query/report"
        );
    } else {
        bounded_host(
            Command::new(root.join("dns-client.exe"))
                .current_dir(root)
                .args(["--exact", CLIENT_TEST, "--nocapture"]),
        )?;
    }
    let bytes = fs::read(output)?;
    ensure!(bytes.len() <= 512, "oversized DNS query result");
    Ok(serde_json::from_slice(&bytes)?)
}

fn creation_time() -> Result<u64> {
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

#[test]
fn actual_lpac_dns_does_not_gain_brokered_egress_from_network_capabilities() -> Result<()> {
    no_nrpt_override()?;
    offline_network::validate_layout()?;
    let profile = random_profile_name()?;
    let supplied =
        std::env::temp_dir().join(format!("bello-dns-ci-{}", offline_network::new_id()?));
    fs::create_dir(&supplied)?;
    let root = fs::canonicalize(&supplied)?;
    let caps = CapabilitySids::for_network(true)?;
    let sid = match create_profile(&profile, &caps) {
        Ok(sid) => sid,
        Err(error) => {
            fs::remove_dir(&root)?;
            return Err(error);
        }
    };
    let mut lease: Option<LeaseRecord> = None;
    let safe_cleanup = Cell::new(true);
    let outcome = (|| -> Result<()> {
        let mut configs = (0..5)
            .map(|_| {
                Ok(QueryConfig {
                    nonce: offline_network::new_id()?.replace('-', ""),
                    port: 1,
                })
            })
            .collect::<Result<Vec<_>>>()?;
        let allowed = configs
            .iter()
            .map(QueryConfig::name)
            .collect::<Result<BTreeSet<_>>>()?;
        let mut peer = Responder::start(allowed)?;
        for config in &mut configs {
            config.port = peer.port;
        }
        fs::copy(std::env::current_exe()?, root.join("dns-client.exe"))?;
        fs::write(root.join(CONFIG), serde_json::to_vec(&configs[0])?)?;
        for path in [&root, &root.join("dns-client.exe"), &root.join(CONFIG)] {
            acl::grant(&open_path(path, true)?, sid.0, SandboxMode::WorkspaceWrite)?;
        }
        let run = |index: usize, child: bool, network: bool| -> Result<QueryResult> {
            fs::write(root.join(CONFIG), serde_json::to_vec(&configs[index])?)?;
            launch(&root, child.then_some(sid.0), network, &safe_cleanup)
        };
        let host = run(0, false, false)?;
        ensure!(host.returned == 0 && host.status == 0 && host.records && peer.saw(&configs[0].name()?)?,
            "INCONCLUSIVE: explicit local DnsQueryEx host control failed (including custom-port support): {host:?}");
        let old = run(1, true, false).context("original no-network-cap LPAC DNS query")?;
        let online = run(2, true, true).context("network-cap LPAC DNS control without WFP")?;
        let record = LeaseRecord {
            policy_version: NETWORK_POLICY_VERSION,
            profile_name: profile.clone(),
            package_sid: sid_string(sid.0)?,
            owner_sid: acl::current_account_sid_string()?,
            caller_pid: std::process::id(),
            caller_creation: creation_time()?,
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
        ensure!(
            installed.filter_ids.iter().all(|id| *id != 0),
            "missing installed WFP filter IDs"
        );
        let offline = run(3, true, true).context("new package-WFP offline LPAC DNS query")?;
        let host_after = run(4, false, false)?;
        ensure!(
            host_after.returned == 0 && host_after.status == 0 && peer.saw(&configs[4].name()?)?,
            "host DNS responder stopped working during negative control: {host_after:?}"
        );
        // Keep the responder alive briefly after all synchronous calls return,
        // catching service work that outlives a denied client request.
        std::thread::sleep(Duration::from_secs(2));
        peer.finish()?;
        let old_packet = peer.saw(&configs[1].name()?)?;
        let online_packet = peer.saw(&configs[2].name()?)?;
        let offline_packet = peer.saw(&configs[3].name()?)?;
        eprintln!("local DNS proof: old_no_cap={old:?}, old_packet={old_packet}; network_cap={online:?}, online_packet={online_packet}; package_wfp={offline:?}, offline_packet={offline_packet}");
        // Packet reception is authoritative even if the API reports an error.
        // This does not claim all DNS/WinRT/HTTP broker paths are covered, nor
        // attribute a block to WFP when the positive LPAC control is blocked.
        ensure!(
            !old_packet,
            "original no-network-cap LPAC also emits DNS; no offline-isolation claim is justified"
        );
        ensure!(!offline_packet, "SECURITY REGRESSION: local responder received DNS from new offline LPAC while old no-cap LPAC was blocked");
        if !online_packet {
            eprintln!("DNS RPC control is unavailable to this LPAC even with network capabilities; this path shows no regression but does not prove WFP blocked DNS service traffic");
        }
        Ok(())
    })();
    ensure!(safe_cleanup.get(), "DNS fixture job could not be proven empty; retaining its profile, files and any blocking lease. Original failure: {outcome:?}");
    let mut errors = Vec::new();
    if let Some(record) = lease {
        if let Err(error) = offline_network::remove_lease(&record) {
            errors.push(format!("lease: {error:#}"));
        }
    }
    if let Err(error) = fs::remove_dir_all(&root) {
        errors.push(format!("fixture: {error}"));
    }
    if let Err(error) = delete_profile(&profile) {
        errors.push(format!("profile: {error:#}"));
    }
    ensure!(
        errors.is_empty(),
        "DNS fixture cleanup failed: {errors:?}; original={outcome:?}"
    );
    outcome
}

#[test]
fn local_dns_responder_rejects_nonfixture_and_malformed_queries() -> Result<()> {
    let config = QueryConfig {
        nonce: "a".repeat(32),
        port: 53,
    };
    let allowed = BTreeSet::from([config.name()?]);
    let mut packet = vec![0x12, 0x34, 1, 0, 0, 1, 0, 0, 0, 0, 0, 0];
    for label in config.name()?.trim_end_matches('.').split('.') {
        packet.push(label.len() as u8);
        packet.extend_from_slice(label.as_bytes());
    }
    packet.extend_from_slice(&[0, 0, 1, 0, 1]);
    let (_, reply) = response(&packet, &allowed).expect("valid local query");
    assert_eq!(&reply[..2], &[0x12, 0x34]);
    assert_eq!(&reply[reply.len() - 4..], &[127, 0, 0, 42]);
    for size in 0..packet.len() {
        assert!(response(&packet[..size], &allowed).is_none());
    }
    assert!(response(&packet, &BTreeSet::new()).is_none());
    packet[12] = 0xc0;
    assert!(response(&packet, &allowed).is_none());
    assert!(QueryConfig {
        nonce: "example.org".into(),
        port: 53
    }
    .name()
    .is_err());
    Ok(())
}
