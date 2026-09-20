//! The broker's fixed, persistent offline policy. This module never chooses
//! commands or permissions. A lease is exactly four package-SID BLOCK filters.
//! Management uses BFE transactions; a malformed/partial lease is retained and
//! reported, never repaired by deleting objects we cannot authenticate.

use crate::identity::{derive_profile_sid, sid_string, validate_profile_name};
use crate::network_protocol::{
    InstalledLease, LeaseRecord, MAX_BROKER_MESSAGE, NETWORK_POLICY_VERSION, SERVICE_NAME,
};
use crate::winutil::{last_error, wide};
use anyhow::{anyhow, bail, Context, Result};
use std::collections::{BTreeMap, BTreeSet};
use std::ffi::c_void;
use std::mem;
use std::ptr;
use windows_sys::core::GUID;
use windows_sys::Win32::Foundation::{
    LocalFree, FWP_E_PROVIDER_NOT_FOUND, FWP_E_SUBLAYER_NOT_FOUND, HANDLE, PSID,
};
use windows_sys::Win32::NetworkManagement::WindowsFilteringPlatform::*;
use windows_sys::Win32::Security::Authorization::{
    ConvertStringSecurityDescriptorToSecurityDescriptorW, ConvertStringSidToSidW,
};
use windows_sys::Win32::Security::Cryptography::{
    BCryptGenRandom, BCRYPT_USE_SYSTEM_PREFERRED_RNG,
};
use windows_sys::Win32::Security::{
    GetAce, GetLengthSid, GetSecurityDescriptorControl, GetSecurityDescriptorDacl,
    GetSecurityDescriptorOwner, IsValidAcl, IsValidSecurityDescriptor, IsValidSid,
    ACCESS_ALLOWED_ACE, ACE_HEADER, ACL, DACL_SECURITY_INFORMATION, OWNER_SECURITY_INFORMATION,
    PSECURITY_DESCRIPTOR, SE_DACL_PROTECTED,
};

pub const PROVIDER_KEY: GUID = GUID::from_u128(0xd46c4bb0_648d_4234_8163_faf9f795547c);
pub const SUBLAYER_KEY: GUID = GUID::from_u128(0xb4eccee6_16b6_4b64_b083_15e3289d7ade);
const LAYERS: [GUID; 4] = [
    FWPM_LAYER_ALE_AUTH_CONNECT_V4,
    FWPM_LAYER_ALE_AUTH_CONNECT_V6,
    FWPM_LAYER_ALE_AUTH_RECV_ACCEPT_V4,
    FWPM_LAYER_ALE_AUTH_RECV_ACCEPT_V6,
];
const PROVIDER_NAME: &str = "Bello offline network v1";
const SUBLAYER_NAME: &str = "Bello package offline isolation v1";
const FILTER_NAME: &str = "Bello offline package lease v1";
const LAYOUT_DATA: &[u8] = b"Bello.OfflineNetwork.Policy.v1";
// Explicit rights, not GENERIC_* bits which BFE may map on insertion. No user
// gets ADD/ADD_LINK/DELETE/WRITE_DAC on these objects or on WFP containers.
const OBJECT_SD: &str = "O:BAG:BAD:P(A;;0x000f07ff;;;SY)(A;;0x000f07ff;;;BA)(A;;0x00020080;;;AU)";
const ADMIN_ACCESS: u32 = 0x000f_07ff;
const READ_ACCESS: u32 = 0x0002_0080;
const MAX_ENUM_FILTERS: usize = 100_000;
const MAX_LEASES: usize = 4096;

fn check(code: u32, operation: &str) -> Result<()> {
    if code != 0 {
        bail!("{operation} failed with Windows/WFP error 0x{code:08x}");
    }
    Ok(())
}

struct LocalAllocation(*mut c_void);
impl Drop for LocalAllocation {
    fn drop(&mut self) {
        if !self.0.is_null() {
            unsafe {
                LocalFree(self.0);
            }
        }
    }
}
struct WfpAllocation<T>(*mut T);
impl<T> Drop for WfpAllocation<T> {
    fn drop(&mut self) {
        if !self.0.is_null() {
            unsafe {
                FwpmFreeMemory0(&mut self.0 as *mut _ as *mut *mut c_void);
            }
        }
    }
}

struct Engine(HANDLE);
impl Engine {
    fn open() -> Result<Self> {
        let mut handle = 0;
        let mut session: FWPM_SESSION0 = unsafe { mem::zeroed() };
        session.txnWaitTimeoutInMSec = 30_000;
        // RPC_C_AUTHN_WINNT, local BFE only; deliberately not a dynamic session.
        check(
            unsafe { FwpmEngineOpen0(ptr::null(), 10, ptr::null(), &session, &mut handle) },
            "FwpmEngineOpen0",
        )?;
        if handle == 0 {
            bail!("BFE returned a null session");
        }
        Ok(Self(handle))
    }
    fn transaction<T>(
        &self,
        read_only: bool,
        action: impl FnOnce(&Self) -> Result<T>,
    ) -> Result<T> {
        check(
            unsafe {
                FwpmTransactionBegin0(self.0, if read_only { FWPM_TXN_READ_ONLY } else { 0 })
            },
            "FwpmTransactionBegin0",
        )?;
        struct Abort<'a>(&'a Engine, bool);
        impl Drop for Abort<'_> {
            fn drop(&mut self) {
                if self.1 {
                    unsafe {
                        FwpmTransactionAbort0(self.0 .0);
                    }
                }
            }
        }
        let mut rollback = Abort(self, true);
        let result = action(self)?;
        check(
            unsafe { FwpmTransactionCommit0(self.0) },
            "FwpmTransactionCommit0",
        )?;
        rollback.1 = false;
        Ok(result)
    }
}
impl Drop for Engine {
    fn drop(&mut self) {
        unsafe {
            FwpmEngineClose0(self.0);
        }
    }
}

fn eq_guid(a: &GUID, b: &GUID) -> bool {
    a.data1 == b.data1 && a.data2 == b.data2 && a.data3 == b.data3 && a.data4 == b.data4
}
fn guid_string(g: &GUID) -> String {
    format!(
        "{:08x}-{:04x}-{:04x}-{:02x}{:02x}-{:02x}{:02x}{:02x}{:02x}{:02x}{:02x}",
        g.data1,
        g.data2,
        g.data3,
        g.data4[0],
        g.data4[1],
        g.data4[2],
        g.data4[3],
        g.data4[4],
        g.data4[5],
        g.data4[6],
        g.data4[7]
    )
}
fn parse_id(value: &str) -> Result<GUID> {
    if value.len() != 36
        || value.bytes().enumerate().any(|(i, b)| {
            if [8, 13, 18, 23].contains(&i) {
                b != b'-'
            } else {
                !b.is_ascii_digit() && !(b'a'..=b'f').contains(&b)
            }
        })
    {
        bail!("expected a canonical lowercase UUID");
    }
    let number = u128::from_str_radix(&value.replace('-', ""), 16).context("invalid UUID")?;
    if number == 0 {
        bail!("zero UUID is not an identity");
    }
    Ok(GUID::from_u128(number))
}

pub fn new_id() -> Result<String> {
    let mut bytes = [0_u8; 16];
    let status = unsafe {
        BCryptGenRandom(
            ptr::null_mut(),
            bytes.as_mut_ptr(),
            bytes.len() as u32,
            BCRYPT_USE_SYSTEM_PREFERRED_RNG,
        )
    };
    if status < 0 {
        bail!("BCryptGenRandom(UUID) failed: 0x{status:08x}");
    }
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    Ok(guid_string(&GUID::from_u128(u128::from_be_bytes(bytes))))
}

pub fn current_boot_id() -> Result<String> {
    #[repr(C)]
    struct BootEnvironment {
        boot_identifier: GUID,
        firmware_type: u32,
        boot_flags: u64,
    }
    #[link(name = "ntdll")]
    extern "system" {
        fn NtQuerySystemInformation(
            class: u32,
            information: *mut c_void,
            length: u32,
            returned: *mut u32,
        ) -> i32;
    }
    let mut boot: BootEnvironment = unsafe { mem::zeroed() };
    let mut returned = 0;
    // SystemBootEnvironmentInformation, Windows 8+ layout. Never substitute a
    // timestamp/uptime guess: an unknown boot must retain old blocking leases.
    let status = unsafe {
        NtQuerySystemInformation(
            90,
            &mut boot as *mut _ as *mut c_void,
            mem::size_of_val(&boot) as u32,
            &mut returned,
        )
    };
    if status < 0 || returned != mem::size_of_val(&boot) as u32 {
        bail!("could not verify Windows boot identity (status 0x{status:08x}, size {returned})");
    }
    let value = guid_string(&boot.boot_identifier);
    parse_id(&value)?;
    Ok(value)
}

fn parse_sid(value: &str) -> Result<LocalAllocation> {
    if value.len() > 184
        || !value.starts_with("S-1-")
        || value
            .bytes()
            .any(|b| !b.is_ascii_digit() && b != b'-' && b != b'S')
    {
        bail!("invalid stored SID");
    }
    let mut sid: PSID = ptr::null_mut();
    if unsafe { ConvertStringSidToSidW(wide(value).as_ptr(), &mut sid) } == 0 {
        return Err(last_error("ConvertStringSidToSidW(offline lease)"));
    }
    let allocation = LocalAllocation(sid);
    if unsafe { IsValidSid(sid) } == 0 || sid_string(sid)? != value {
        bail!("noncanonical stored SID");
    }
    Ok(allocation)
}

fn record_bytes(record: &LeaseRecord) -> Result<Vec<u8>> {
    if record.policy_version != NETWORK_POLICY_VERSION
        || record.caller_pid == 0
        || record.caller_creation == 0
    {
        bail!("invalid offline lease version/caller identity");
    }
    validate_profile_name(&record.profile_name)?;
    let expected = derive_profile_sid(&record.profile_name)?;
    if sid_string(expected.0)? != record.package_sid {
        bail!("lease package does not match its Bello profile");
    }
    let _owner = parse_sid(&record.owner_sid)?;
    parse_id(&record.boot_id)?;
    parse_id(&record.lease_id)?;
    let mut keys = BTreeSet::new();
    for value in &record.filter_keys {
        let key = parse_id(value)?;
        if eq_guid(&key, &PROVIDER_KEY) || eq_guid(&key, &SUBLAYER_KEY) || !keys.insert(value) {
            bail!("invalid or duplicate lease filter key");
        }
    }
    let encoded = serde_json::to_vec(record)?;
    if encoded.len() > MAX_BROKER_MESSAGE {
        bail!("offline lease exceeds provider-data limit");
    }
    Ok(encoded)
}

// WFP owns and validates these buffers. All helper reads are additionally
// bounded; no pointer from the local broker protocol is ever interpreted here.
unsafe fn blob_bytes(blob: &FWP_BYTE_BLOB, maximum: usize) -> Result<&[u8]> {
    if blob.size as usize > maximum || (blob.size != 0 && blob.data.is_null()) {
        bail!("invalid WFP provider data size");
    }
    if blob.size == 0 {
        return Ok(&[]);
    }
    Ok(std::slice::from_raw_parts(blob.data, blob.size as usize))
}
unsafe fn string_equals(value: *const u16, expected: &str) -> bool {
    if value.is_null() {
        return false;
    }
    wide(expected)
        .iter()
        .enumerate()
        .all(|(i, c)| *value.add(i) == *c)
}
fn data_blob(bytes: &[u8]) -> FWP_BYTE_BLOB {
    FWP_BYTE_BLOB {
        size: bytes.len() as u32,
        data: bytes.as_ptr() as *mut u8,
    }
}
fn descriptor() -> Result<LocalAllocation> {
    let mut sd = ptr::null_mut();
    if unsafe {
        ConvertStringSecurityDescriptorToSecurityDescriptorW(
            wide(OBJECT_SD).as_ptr(),
            1,
            &mut sd,
            ptr::null_mut(),
        )
    } == 0
    {
        return Err(last_error("offline network security descriptor"));
    }
    Ok(LocalAllocation(sd))
}

type SecurityGetter = unsafe extern "system" fn(
    HANDLE,
    *const GUID,
    u32,
    *mut PSID,
    *mut PSID,
    *mut *mut ACL,
    *mut *mut ACL,
    *mut PSECURITY_DESCRIPTOR,
) -> u32;
fn validate_security(engine: &Engine, key: &GUID, getter: SecurityGetter) -> Result<()> {
    let mut sd = ptr::null_mut();
    check(
        unsafe {
            getter(
                engine.0,
                key,
                OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
                ptr::null_mut(),
                ptr::null_mut(),
                ptr::null_mut(),
                ptr::null_mut(),
                &mut sd,
            )
        },
        "read offline WFP object security",
    )?;
    let allocation = WfpAllocation(sd);
    if allocation.0.is_null() || unsafe { IsValidSecurityDescriptor(sd) } == 0 {
        bail!("invalid WFP security descriptor");
    }
    let mut owner = ptr::null_mut();
    let mut defaulted = 0;
    if unsafe { GetSecurityDescriptorOwner(sd, &mut owner, &mut defaulted) } == 0 || owner.is_null()
    {
        bail!("missing WFP object owner");
    }
    if !matches!(sid_string(owner)?.as_str(), "S-1-5-18" | "S-1-5-32-544") {
        bail!("offline WFP object is not administrator-owned");
    }
    let mut control = 0;
    let mut revision = 0;
    let mut present = 0;
    let mut acl = ptr::null_mut();
    if unsafe { GetSecurityDescriptorControl(sd, &mut control, &mut revision) } == 0
        || control & SE_DACL_PROTECTED == 0
        || unsafe { GetSecurityDescriptorDacl(sd, &mut present, &mut acl, &mut defaulted) } == 0
        || present == 0
        || acl.is_null()
        || unsafe { IsValidAcl(acl) } == 0
        || unsafe { (*acl).AceCount } != 3
    {
        bail!("offline WFP DACL is not the fixed protected policy");
    }
    let expected = [
        ("S-1-5-18", ADMIN_ACCESS),
        ("S-1-5-32-544", ADMIN_ACCESS),
        ("S-1-5-11", READ_ACCESS),
    ];
    for (index, (sid, mask)) in expected.iter().enumerate() {
        let mut ace: *mut c_void = ptr::null_mut();
        if unsafe { GetAce(acl, index as u32, &mut ace) } == 0 || ace.is_null() {
            bail!("cannot inspect WFP DACL entry");
        }
        let header = unsafe { &*(ace as *const ACE_HEADER) };
        if header.AceType != 0 || header.AceFlags != 0 || header.AceSize < 16 {
            bail!("unexpected WFP DACL entry type/flags");
        }
        let entry = unsafe { &*(ace as *const ACCESS_ALLOWED_ACE) };
        let entry_sid = ptr::addr_of!(entry.SidStart) as PSID;
        if unsafe { IsValidSid(entry_sid) } == 0
            || header.AceSize as u32 != 8 + unsafe { GetLengthSid(entry_sid) }
            || entry.Mask != *mask
            || sid_string(entry_sid)? != *sid
        {
            bail!("unexpected WFP DACL trustee or rights");
        }
    }
    Ok(())
}

type LayoutObjects = (
    Option<WfpAllocation<FWPM_PROVIDER0>>,
    Option<WfpAllocation<FWPM_SUBLAYER0>>,
);
fn layout_objects(engine: &Engine) -> Result<LayoutObjects> {
    let mut provider = ptr::null_mut();
    let code = unsafe { FwpmProviderGetByKey0(engine.0, &PROVIDER_KEY, &mut provider) };
    let provider = if code == FWP_E_PROVIDER_NOT_FOUND as u32 {
        None
    } else {
        check(code, "FwpmProviderGetByKey0")?;
        if provider.is_null() {
            bail!("null WFP provider");
        }
        Some(WfpAllocation(provider))
    };
    let mut sublayer = ptr::null_mut();
    let code = unsafe { FwpmSubLayerGetByKey0(engine.0, &SUBLAYER_KEY, &mut sublayer) };
    let sublayer = if code == FWP_E_SUBLAYER_NOT_FOUND as u32 {
        None
    } else {
        check(code, "FwpmSubLayerGetByKey0")?;
        if sublayer.is_null() {
            bail!("null WFP sublayer");
        }
        Some(WfpAllocation(sublayer))
    };
    Ok((provider, sublayer))
}
fn validate_layout_in(engine: &Engine) -> Result<()> {
    let (provider, sublayer) = layout_objects(engine)?;
    let provider =
        provider.ok_or_else(|| anyhow!("Bello offline WFP provider is not installed"))?;
    let sublayer =
        sublayer.ok_or_else(|| anyhow!("Bello offline WFP sublayer is not installed"))?;
    let p = unsafe { &*provider.0 };
    let s = unsafe { &*sublayer.0 };
    let valid = unsafe {
        eq_guid(&p.providerKey, &PROVIDER_KEY)
            && p.flags == FWPM_PROVIDER_FLAG_PERSISTENT
            && string_equals(p.displayData.name, PROVIDER_NAME)
            && p.displayData.description.is_null()
            && string_equals(p.serviceName, SERVICE_NAME)
            && blob_bytes(&p.providerData, LAYOUT_DATA.len())? == LAYOUT_DATA
            && eq_guid(&s.subLayerKey, &SUBLAYER_KEY)
            && s.flags == FWPM_SUBLAYER_FLAG_PERSISTENT
            && !s.providerKey.is_null()
            && eq_guid(&*s.providerKey, &PROVIDER_KEY)
            && s.weight == u16::MAX
            && string_equals(s.displayData.name, SUBLAYER_NAME)
            && s.displayData.description.is_null()
            && blob_bytes(&s.providerData, LAYOUT_DATA.len())? == LAYOUT_DATA
    };
    if !valid {
        bail!("existing offline WFP layout differs from the fixed policy; refusing repair");
    }
    validate_security(engine, &PROVIDER_KEY, FwpmProviderGetSecurityInfoByKey0)?;
    validate_security(engine, &SUBLAYER_KEY, FwpmSubLayerGetSecurityInfoByKey0)
}

/// Installer only: creates both objects atomically, or verifies both unchanged.
pub fn ensure_layout() -> Result<()> {
    let engine = Engine::open()?;
    engine.transaction(false, |engine| {
        match layout_objects(engine)? {
            (Some(_), Some(_)) => return validate_layout_in(engine),
            (None, None) => {}
            _ => bail!("partial offline WFP layout; refusing automatic repair"),
        }
        let sd = descriptor()?;
        let mut provider: FWPM_PROVIDER0 = unsafe { mem::zeroed() };
        let mut pname = wide(PROVIDER_NAME);
        let mut service = wide(SERVICE_NAME);
        provider.providerKey = PROVIDER_KEY;
        provider.displayData.name = pname.as_mut_ptr();
        provider.flags = FWPM_PROVIDER_FLAG_PERSISTENT;
        provider.providerData = data_blob(LAYOUT_DATA);
        provider.serviceName = service.as_mut_ptr();
        check(
            unsafe { FwpmProviderAdd0(engine.0, &provider, sd.0) },
            "FwpmProviderAdd0",
        )?;
        let mut sublayer: FWPM_SUBLAYER0 = unsafe { mem::zeroed() };
        let mut sname = wide(SUBLAYER_NAME);
        sublayer.subLayerKey = SUBLAYER_KEY;
        sublayer.displayData.name = sname.as_mut_ptr();
        sublayer.flags = FWPM_SUBLAYER_FLAG_PERSISTENT;
        sublayer.providerKey = &mut provider.providerKey;
        sublayer.providerData = data_blob(LAYOUT_DATA);
        sublayer.weight = u16::MAX;
        check(
            unsafe { FwpmSubLayerAdd0(engine.0, &sublayer, sd.0) },
            "FwpmSubLayerAdd0",
        )?;
        validate_layout_in(engine)
    })
}

/// Read-only check; unlike the installer this never fills in missing objects.
pub fn validate_layout() -> Result<()> {
    validate_layout_in(&Engine::open()?)
}

fn filter_record(filter: &FWPM_FILTER0) -> Result<(LeaseRecord, usize)> {
    let encoded = unsafe { blob_bytes(&filter.providerData, MAX_BROKER_MESSAGE)? };
    let record: LeaseRecord =
        serde_json::from_slice(encoded).context("unrecognized offline WFP lease record")?;
    if record_bytes(&record)? != encoded {
        bail!("noncanonical offline lease provider data");
    }
    let key_text = guid_string(&filter.filterKey);
    let index = record
        .filter_keys
        .iter()
        .position(|key| *key == key_text)
        .ok_or_else(|| anyhow!("lease record does not name this filter"))?;
    validate_filter_shape(filter, index)?;
    let condition = unsafe { &*filter.filterCondition };
    if !eq_guid(&condition.fieldKey, &FWPM_CONDITION_ALE_PACKAGE_ID)
        || condition.matchType != FWP_MATCH_EQUAL
        || condition.conditionValue.r#type != FWP_SID
    {
        bail!("offline filter does not match exact package SID");
    }
    let sid = unsafe { condition.conditionValue.Anonymous.sid } as PSID;
    if sid.is_null() || unsafe { IsValidSid(sid) } == 0 || sid_string(sid)? != record.package_sid {
        bail!("offline filter package SID differs from lease");
    }
    Ok((record, index))
}

fn validate_filter_shape(filter: &FWPM_FILTER0, index: usize) -> Result<()> {
    // A returned FWPM_FILTER0 has both submitted and BFE-assigned members.
    // Report exactly which bounded scalar differs; do not dump providerData,
    // display strings, pointers, or account/package identities into logs. This
    // permits only the INDEXED optimization observed in native BFE readback.
    // INDEXED changes lookup performance, not the action or matching scope:
    // https://learn.microsoft.com/windows/win32/api/fwpmtypes/ns-fwpmtypes-fwpm_filter0
    let mut mismatches = Vec::new();
    if filter.flags != FWPM_FILTER_FLAG_PERSISTENT
        && filter.flags != FWPM_FILTER_FLAG_PERSISTENT | FWPM_FILTER_FLAG_INDEXED
    {
        mismatches.push(format!(
            "flags=0x{:08x}, expected=0x{:08x} or 0x{:08x}",
            filter.flags,
            FWPM_FILTER_FLAG_PERSISTENT,
            FWPM_FILTER_FLAG_PERSISTENT | FWPM_FILTER_FLAG_INDEXED
        ));
    }
    if filter.providerKey.is_null() || !unsafe { eq_guid(&*filter.providerKey, &PROVIDER_KEY) } {
        mismatches.push("providerKey mismatch".into());
    }
    if !eq_guid(&filter.subLayerKey, &SUBLAYER_KEY) {
        mismatches.push("subLayerKey mismatch".into());
    }
    if !eq_guid(&filter.layerKey, &LAYERS[index]) {
        mismatches.push(format!("layerKey mismatch for slot={index}"));
    }
    if filter.action.r#type != FWP_ACTION_BLOCK {
        mismatches.push(format!(
            "action=0x{:08x}, expected=0x{:08x}",
            filter.action.r#type, FWP_ACTION_BLOCK
        ));
    }
    if unsafe { filter.Anonymous.rawContext } != 0 {
        mismatches.push("rawContext is nonzero".into());
    }
    if filter.numFilterConditions != 1 || filter.filterCondition.is_null() {
        mismatches.push(format!(
            "condition count={}, pointerPresent={}",
            filter.numFilterConditions,
            !filter.filterCondition.is_null()
        ));
    }
    for (label, value) in [
        ("weight", &filter.weight),
        ("effectiveWeight", &filter.effectiveWeight),
    ] {
        let scalar = if value.r#type == FWP_UINT64 && !unsafe { value.Anonymous.uint64 }.is_null() {
            Some(unsafe { *value.Anonymous.uint64 })
        } else {
            None
        };
        if scalar != Some(u64::MAX) {
            mismatches.push(format!(
                "{label}: type={}, uint64={scalar:?}, expectedType={FWP_UINT64}, expectedUint64={}",
                value.r#type,
                u64::MAX
            ));
        }
    }
    if filter.filterId == 0 {
        mismatches.push("filterId is zero".into());
    }
    if !unsafe { string_equals(filter.displayData.name, FILTER_NAME) } {
        mismatches.push("displayData.name mismatch".into());
    }
    if !filter.displayData.description.is_null() {
        mismatches.push("displayData.description is present".into());
    }
    if !mismatches.is_empty() {
        bail!(
            "offline lease filter has an unexpected policy shape (slot={index}): {}",
            mismatches.join("; ")
        );
    }
    Ok(())
}

fn leases_in(engine: &Engine) -> Result<Vec<InstalledLease>> {
    let mut enumeration = 0;
    // Inspect all filters so foreign provider entries in OUR sublayer cannot
    // hide from validation. Foreign objects are never changed or logged.
    check(
        unsafe { FwpmFilterCreateEnumHandle0(engine.0, ptr::null(), &mut enumeration) },
        "FwpmFilterCreateEnumHandle0",
    )?;
    struct Enumeration<'a>(&'a Engine, HANDLE);
    impl Drop for Enumeration<'_> {
        fn drop(&mut self) {
            unsafe {
                FwpmFilterDestroyEnumHandle0(self.0 .0, self.1);
            }
        }
    }
    let enumeration = Enumeration(engine, enumeration);
    let mut result: BTreeMap<String, InstalledLease> = BTreeMap::new();
    let mut total = 0_usize;
    loop {
        let mut entries: *mut *mut FWPM_FILTER0 = ptr::null_mut();
        let mut count = 0;
        check(
            unsafe { FwpmFilterEnum0(engine.0, enumeration.1, 256, &mut entries, &mut count) },
            "FwpmFilterEnum0",
        )?;
        let _allocation = WfpAllocation(entries);
        if count > 256 || (count > 0 && entries.is_null()) {
            bail!("invalid WFP enumeration batch");
        }
        if count == 0 {
            break;
        }
        total += count as usize;
        if total > MAX_ENUM_FILTERS {
            bail!("WFP filter enumeration exceeds supported bound");
        }
        for index in 0..count as usize {
            let raw = unsafe { *entries.add(index) };
            if raw.is_null() {
                bail!("null WFP filter entry");
            }
            let filter = unsafe { &*raw };
            let ours = eq_guid(&filter.subLayerKey, &SUBLAYER_KEY)
                || (!filter.providerKey.is_null()
                    && unsafe { eq_guid(&*filter.providerKey, &PROVIDER_KEY) });
            if !ours {
                continue;
            }
            let (record, slot) = filter_record(filter)?;
            validate_security(engine, &filter.filterKey, FwpmFilterGetSecurityInfoByKey0)?;
            let lease = result
                .entry(record.lease_id.clone())
                .or_insert_with(|| InstalledLease {
                    record: record.clone(),
                    filter_ids: [0; 4],
                });
            if lease.record != record || lease.filter_ids[slot] != 0 {
                bail!("inconsistent or duplicate offline lease records");
            }
            lease.filter_ids[slot] = filter.filterId;
            if result.len() > MAX_LEASES {
                bail!("too many retained offline leases");
            }
        }
    }
    let mut packages = BTreeSet::new();
    for lease in result.values() {
        if lease.filter_ids.contains(&0) {
            bail!("partial offline lease retained; expected four exact filters");
        }
        if !packages.insert(&lease.record.package_sid) {
            bail!("multiple leases claim the same package");
        }
    }
    Ok(result.into_values().collect())
}

pub fn leases() -> Result<Vec<InstalledLease>> {
    let engine = Engine::open()?;
    engine.transaction(true, |engine| {
        validate_layout_in(engine)?;
        leases_in(engine)
    })
}

pub fn install_lease(record: LeaseRecord) -> Result<InstalledLease> {
    let encoded = record_bytes(&record)?;
    if record.boot_id != current_boot_id()? {
        bail!("cannot install a lease for a different Windows boot");
    }
    let sid = derive_profile_sid(&record.profile_name)?;
    let engine = Engine::open()?;
    engine.transaction(false, |engine| {
        validate_layout_in(engine)?;
        let old = leases_in(engine)?;
        for lease in &old {
            if lease.record == record {
                return Ok(lease.clone());
            }
            if lease.record.lease_id == record.lease_id
                || lease.record.package_sid == record.package_sid
            {
                bail!("offline lease identity already exists with different data");
            }
        }
        if old.len() >= MAX_LEASES {
            bail!("offline lease capacity reached; retained leases were not deleted");
        }
        let sd = descriptor()?;
        let mut ids = [0_u64; 4];
        let mut name = wide(FILTER_NAME);
        let mut provider = PROVIDER_KEY;
        let mut weight = u64::MAX;
        for index in 0..4 {
            let mut condition: FWPM_FILTER_CONDITION0 = unsafe { mem::zeroed() };
            condition.fieldKey = FWPM_CONDITION_ALE_PACKAGE_ID;
            condition.matchType = FWP_MATCH_EQUAL;
            condition.conditionValue.r#type = FWP_SID;
            condition.conditionValue.Anonymous.sid = sid.0 as *mut _;
            let mut filter: FWPM_FILTER0 = unsafe { mem::zeroed() };
            filter.filterKey = parse_id(&record.filter_keys[index])?;
            filter.displayData.name = name.as_mut_ptr();
            filter.flags = FWPM_FILTER_FLAG_PERSISTENT;
            filter.providerKey = &mut provider;
            filter.providerData = data_blob(&encoded);
            filter.layerKey = LAYERS[index];
            filter.subLayerKey = SUBLAYER_KEY;
            filter.weight.r#type = FWP_UINT64;
            filter.weight.Anonymous.uint64 = &mut weight;
            filter.numFilterConditions = 1;
            filter.filterCondition = &mut condition;
            filter.action.r#type = FWP_ACTION_BLOCK;
            check(
                unsafe { FwpmFilterAdd0(engine.0, &filter, sd.0, &mut ids[index]) },
                "FwpmFilterAdd0(offline lease)",
            )?;
        }
        // Verify the complete object set and every SD before committing/ACK.
        let installed = leases_in(engine)?
            .into_iter()
            .find(|lease| lease.record == record)
            .ok_or_else(|| anyhow!("new offline lease was not visible in transaction"))?;
        if installed.filter_ids != ids {
            bail!("offline filter readback IDs differ");
        }
        Ok(installed)
    })
}

/// Broker must establish Job empty or a different verified boot BEFORE calling.
/// A record is an exact deletion selector, not authorization from a client.
pub fn remove_lease(record: &LeaseRecord) -> Result<()> {
    record_bytes(record)?;
    let engine = Engine::open()?;
    engine.transaction(false, |engine| {
        validate_layout_in(engine)?;
        let installed = leases_in(engine)?;
        let found = installed
            .iter()
            .find(|lease| lease.record.lease_id == record.lease_id);
        let Some(found) = found else {
            // Same keys reused in another record must not become an idempotent
            // success. The complete enumeration above authenticated all ours.
            if installed.iter().any(|lease| {
                lease
                    .record
                    .filter_keys
                    .iter()
                    .any(|key| record.filter_keys.contains(key))
            }) {
                bail!("lease keys belong to a different record");
            }
            return Ok(());
        };
        if &found.record != record {
            bail!("refusing deletion of a different offline lease");
        }
        for key in &record.filter_keys {
            check(
                unsafe { FwpmFilterDeleteByKey0(engine.0, &parse_id(key)?) },
                "FwpmFilterDeleteByKey0(offline lease)",
            )?;
        }
        if leases_in(engine)?
            .iter()
            .any(|lease| lease.record.lease_id == record.lease_id)
        {
            bail!("offline lease still exists after removal");
        }
        Ok(())
    })
}

/// Installer only. Never purge live/retained leases or unrelated WFP objects.
pub fn remove_layout() -> Result<()> {
    let engine = Engine::open()?;
    engine.transaction(false, |engine| {
        if matches!(layout_objects(engine)?, (None, None)) {
            return Ok(());
        }
        validate_layout_in(engine)?;
        if !leases_in(engine)?.is_empty() {
            bail!("offline WFP layout has active/retained leases; refusing removal");
        }
        check(
            unsafe { FwpmSubLayerDeleteByKey0(engine.0, &SUBLAYER_KEY) },
            "FwpmSubLayerDeleteByKey0",
        )?;
        check(
            unsafe { FwpmProviderDeleteByKey0(engine.0, &PROVIDER_KEY) },
            "FwpmProviderDeleteByKey0",
        )?;
        Ok(())
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn uuids_are_canonical_distinct_and_nonzero() {
        let one = new_id().unwrap();
        let two = new_id().unwrap();
        assert_ne!(one, two);
        assert_eq!(guid_string(&parse_id(&one).unwrap()), one);
        assert_eq!(&one[14..15], "4");
        for bad in [
            "",
            "00000000-0000-0000-0000-000000000000",
            "D46C4BB0-648D-4234-8163-FAF9F795547C",
            "{d46c4bb0-648d-4234-8163-faf9f795547c}",
            "d46c4bb0/648d-4234-8163-faf9f795547c",
        ] {
            assert!(parse_id(bad).is_err(), "{bad}");
        }
    }
    #[test]
    fn current_boot_identity_is_stable() {
        assert_eq!(current_boot_id().unwrap(), current_boot_id().unwrap());
    }
    fn record() -> LeaseRecord {
        let profile_name = crate::identity::random_profile_name().unwrap();
        LeaseRecord {
            policy_version: NETWORK_POLICY_VERSION,
            package_sid: sid_string(derive_profile_sid(&profile_name).unwrap().0).unwrap(),
            profile_name,
            owner_sid: "S-1-5-18".into(),
            caller_pid: 1,
            caller_creation: 1,
            boot_id: new_id().unwrap(),
            lease_id: new_id().unwrap(),
            filter_keys: [
                new_id().unwrap(),
                new_id().unwrap(),
                new_id().unwrap(),
                new_id().unwrap(),
            ],
        }
    }
    #[test]
    fn lease_record_is_bounded_and_binds_profile_package_and_keys() {
        let valid = record();
        assert!(record_bytes(&valid).is_ok());
        let mut bad = valid.clone();
        bad.filter_keys[1] = bad.filter_keys[0].clone();
        assert!(record_bytes(&bad).is_err());
        let mut bad = valid.clone();
        bad.package_sid = "S-1-5-18".into();
        assert!(record_bytes(&bad).is_err());
        let mut bad = valid.clone();
        bad.policy_version += 1;
        assert!(record_bytes(&bad).is_err());
        let mut bad = valid.clone();
        bad.caller_creation = 0;
        assert!(record_bytes(&bad).is_err());
        let mut bad = valid.clone();
        bad.owner_sid = "BA".into();
        assert!(record_bytes(&bad).is_err());
        let mut bad = valid;
        bad.filter_keys[0] = guid_string(&PROVIDER_KEY);
        assert!(record_bytes(&bad).is_err());
    }

    #[test]
    fn filter_shape_rejects_policy_broadening_or_partial_identity() {
        let record = record();
        let bytes = record_bytes(&record).unwrap();
        let sid = derive_profile_sid(&record.profile_name).unwrap();
        let mut name = wide(FILTER_NAME);
        let mut provider = PROVIDER_KEY;
        let mut weight = u64::MAX;
        let mut condition: FWPM_FILTER_CONDITION0 = unsafe { mem::zeroed() };
        condition.fieldKey = FWPM_CONDITION_ALE_PACKAGE_ID;
        condition.matchType = FWP_MATCH_EQUAL;
        condition.conditionValue.r#type = FWP_SID;
        condition.conditionValue.Anonymous.sid = sid.0 as *mut _;
        let mut filter: FWPM_FILTER0 = unsafe { mem::zeroed() };
        filter.filterKey = parse_id(&record.filter_keys[0]).unwrap();
        filter.displayData.name = name.as_mut_ptr();
        filter.flags = FWPM_FILTER_FLAG_PERSISTENT;
        filter.providerKey = &mut provider;
        filter.providerData = data_blob(&bytes);
        filter.layerKey = LAYERS[0];
        filter.subLayerKey = SUBLAYER_KEY;
        filter.weight.r#type = FWP_UINT64;
        filter.weight.Anonymous.uint64 = &mut weight;
        filter.effectiveWeight = filter.weight;
        filter.numFilterConditions = 1;
        filter.filterCondition = &mut condition;
        filter.action.r#type = FWP_ACTION_BLOCK;
        filter.filterId = 1;
        assert_eq!(filter_record(&filter).unwrap().0, record);
        let mut bad = filter;
        bad.action.r#type = FWP_ACTION_PERMIT;
        assert!(filter_record(&bad).is_err());
        let mut bad = filter;
        bad.flags |= FWPM_FILTER_FLAG_DISABLED;
        let error = filter_record(&bad).unwrap_err().to_string();
        assert!(error.contains("flags=0x"));
        assert!(error.len() < 512);
        assert!(!error.contains(&record.package_sid));
        assert!(!error.contains(&record.owner_sid));
        let mut bad = filter;
        bad.flags = 0;
        assert!(filter_record(&bad).is_err());
        let mut indexed = filter;
        indexed.flags |= FWPM_FILTER_FLAG_INDEXED;
        assert_eq!(filter_record(&indexed).unwrap().0, record);
        for flags in [
            FWPM_FILTER_FLAG_INDEXED,
            FWPM_FILTER_FLAG_PERSISTENT | FWPM_FILTER_FLAG_INDEXED | FWPM_FILTER_FLAG_DISABLED,
            FWPM_FILTER_FLAG_PERSISTENT | FWPM_FILTER_FLAG_INDEXED | 0x8000_0000,
            FWPM_FILTER_FLAG_PERSISTENT | FWPM_FILTER_FLAG_CLEAR_ACTION_RIGHT,
        ] {
            let mut bad = filter;
            bad.flags = flags;
            assert!(filter_record(&bad)
                .unwrap_err()
                .to_string()
                .contains("flags=0x"));
        }
        let mut bad = filter;
        bad.numFilterConditions = 0;
        assert!(filter_record(&bad).is_err());
        let mut bad = filter;
        bad.numFilterConditions = 2;
        assert!(filter_record(&bad).is_err());
        let mut bad = filter;
        bad.filterCondition = ptr::null_mut();
        assert!(filter_record(&bad).is_err());
        let mut bad = filter;
        bad.providerKey = ptr::null_mut();
        assert!(filter_record(&bad).is_err());
        let mut bad = filter;
        bad.layerKey = LAYERS[1];
        assert!(filter_record(&bad).is_err());
        let mut bad = filter;
        bad.subLayerKey = PROVIDER_KEY;
        assert!(filter_record(&bad).is_err());
        let mut bad = filter;
        bad.filterKey = parse_id(&new_id().unwrap()).unwrap();
        assert!(filter_record(&bad).is_err());
        let mut bad = filter;
        bad.weight.r#type = FWP_EMPTY;
        assert!(filter_record(&bad).is_err());
        let mut bad = filter;
        bad.effectiveWeight.r#type = FWP_EMPTY;
        let error = filter_record(&bad).unwrap_err().to_string();
        assert!(error.contains("effectiveWeight: type="));
        assert!(error.contains("uint64=None"));
        let mut bad = filter;
        let mut smaller_weight = u64::MAX - 1;
        bad.effectiveWeight.Anonymous.uint64 = &mut smaller_weight;
        assert!(filter_record(&bad)
            .unwrap_err()
            .to_string()
            .contains("effectiveWeight: type="));
        let mut bad = filter;
        bad.weight.Anonymous.uint64 = ptr::null_mut();
        assert!(filter_record(&bad)
            .unwrap_err()
            .to_string()
            .contains("weight: type="));
        let mut bad = filter;
        bad.flags |= FWPM_FILTER_FLAG_INDEXED | FWPM_FILTER_FLAG_DISABLED;
        bad.effectiveWeight.r#type = FWP_EMPTY;
        let error = filter_record(&bad).unwrap_err().to_string();
        assert!(error.contains("flags=0x") && error.contains("effectiveWeight:"));
        assert!(error.len() < 512);
        let mut bad = filter;
        bad.Anonymous.rawContext = 1;
        assert!(filter_record(&bad).is_err());
        let mut bad = filter;
        bad.providerData.size = MAX_BROKER_MESSAGE as u32 + 1;
        assert!(filter_record(&bad).is_err());
        unsafe {
            (*filter.filterCondition).matchType = FWP_MATCH_NOT_EQUAL;
        }
        assert!(filter_record(&filter).is_err());
        unsafe {
            (*filter.filterCondition).matchType = FWP_MATCH_EQUAL;
            (*filter.filterCondition).fieldKey = FWPM_CONDITION_ALE_USER_ID;
        }
        assert!(filter_record(&filter).is_err());
        unsafe {
            (*filter.filterCondition).fieldKey = FWPM_CONDITION_ALE_PACKAGE_ID;
            (*filter.filterCondition).conditionValue.Anonymous.sid = ptr::null_mut();
        }
        assert!(filter_record(&filter).is_err());
    }
}
