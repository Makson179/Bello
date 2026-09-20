use crate::winutil::{last_error, wide, wide_ptr_to_os_string};
use anyhow::{anyhow, Context, Result};
use std::ffi::c_void;
use std::path::PathBuf;
use windows_sys::Win32::Foundation::{LocalFree, PSID};
use windows_sys::Win32::Security::Authorization::ConvertSidToStringSidW;
use windows_sys::Win32::Security::Cryptography::{
    BCryptGenRandom, BCRYPT_USE_SYSTEM_PREFERRED_RNG,
};
use windows_sys::Win32::Security::Isolation::{
    CreateAppContainerProfile, DeleteAppContainerProfile,
    DeriveAppContainerSidFromAppContainerName, GetAppContainerFolderPath,
};
use windows_sys::Win32::Security::{
    DeriveCapabilitySidsFromName, FreeSid, SECURITY_CAPABILITIES, SID_AND_ATTRIBUTES,
};
use windows_sys::Win32::System::Com::CoTaskMemFree;
use windows_sys::Win32::System::SystemServices::SE_GROUP_ENABLED;

pub const PROFILE_PREFIX: &str = "Bello.Sandbox.";
// A named capability is a permission recipient, not proof of Bello identity:
// other host programs can derive/request it. Its only prepared permission is
// non-inheriting metadata access on the fixed OS host-preparation targets.
pub const SYSTEM_ROOT_METADATA_CAPABILITY: &str = "Bello.Sandbox.SystemRootMetadata.v1";
// Only explicit host preparation grants this recipient access to the null
// device. It carries no general filesystem or other device permissions.
pub const NULL_DEVICE_CAPABILITY: &str = "Bello.Sandbox.NullDevice.v1";

pub struct AppContainerSid(pub PSID);

unsafe impl Send for AppContainerSid {}
unsafe impl Sync for AppContainerSid {}

impl Drop for AppContainerSid {
    fn drop(&mut self) {
        if !self.0.is_null() {
            unsafe {
                FreeSid(self.0);
            }
        }
    }
}

pub struct CapabilitySids {
    owned: Vec<PSID>,
    attributes: Vec<SID_AND_ATTRIBUTES>,
}

impl CapabilitySids {
    pub fn for_network(enabled: bool) -> Result<Self> {
        let mut value = Self {
            owned: Vec::new(),
            attributes: Vec::new(),
        };
        for name in [
            "registryRead",
            "lpacCom",
            SYSTEM_ROOT_METADATA_CAPABILITY,
            NULL_DEVICE_CAPABILITY,
        ] {
            value.add_named(name)?;
        }
        if enabled {
            // Together these deliberately authorize public and private network
            // traffic. AppContainer loopback remains blocked unless the host has
            // independently installed a loopback exemption; Bello never does so.
            for name in ["internetClientServer", "privateNetworkClientServer"] {
                value.add_named(name)?;
            }
        }
        Ok(value)
    }

    pub fn system_root_metadata() -> Result<Self> {
        let mut value = Self {
            owned: Vec::new(),
            attributes: Vec::new(),
        };
        value.add_named(SYSTEM_ROOT_METADATA_CAPABILITY)?;
        if value.owned.len() != 1 {
            return Err(anyhow!(
                "system-root metadata capability must resolve to one SID"
            ));
        }
        Ok(value)
    }

    pub fn single_sid(&self) -> Result<PSID> {
        match self.owned.as_slice() {
            [sid] => Ok(*sid),
            _ => Err(anyhow!("expected a single capability SID")),
        }
    }

    pub fn null_device() -> Result<Self> {
        let mut value = Self {
            owned: Vec::new(),
            attributes: Vec::new(),
        };
        value.add_named(NULL_DEVICE_CAPABILITY)?;
        value.single_sid()?;
        Ok(value)
    }

    fn add_named(&mut self, name: &str) -> Result<()> {
        let mut group_sids: *mut PSID = std::ptr::null_mut();
        let mut group_count = 0_u32;
        let mut capability_sids: *mut PSID = std::ptr::null_mut();
        let mut capability_count = 0_u32;
        let ok = unsafe {
            DeriveCapabilitySidsFromName(
                wide(name).as_ptr(),
                &mut group_sids,
                &mut group_count,
                &mut capability_sids,
                &mut capability_count,
            )
        };
        if ok == 0 {
            return Err(last_error(&format!("DeriveCapabilitySidsFromName({name})")));
        }
        unsafe {
            free_sid_array(group_sids, group_count);
        }
        if capability_count == 0 || capability_sids.is_null() {
            return Err(anyhow!("capability {name} resolved to no SID"));
        }
        let mut resolved = Vec::with_capacity(capability_count as usize);
        unsafe {
            for index in 0..capability_count as usize {
                let sid = *capability_sids.add(index);
                if sid.is_null() {
                    free_sid_array(capability_sids, capability_count);
                    return Err(anyhow!("capability {name} returned a null SID"));
                }
                resolved.push(sid);
            }
            LocalFree(capability_sids as *mut c_void);
        }
        self.owned.extend(resolved);
        self.rebuild_attributes();
        Ok(())
    }

    fn rebuild_attributes(&mut self) {
        self.attributes = self
            .owned
            .iter()
            .map(|sid| SID_AND_ATTRIBUTES {
                Sid: *sid,
                Attributes: SE_GROUP_ENABLED as u32,
            })
            .collect();
    }

    pub fn profile_slice(&self) -> *const SID_AND_ATTRIBUTES {
        if self.attributes.is_empty() {
            std::ptr::null()
        } else {
            self.attributes.as_ptr()
        }
    }

    pub fn security_capabilities(&mut self, appcontainer_sid: PSID) -> SECURITY_CAPABILITIES {
        SECURITY_CAPABILITIES {
            AppContainerSid: appcontainer_sid,
            Capabilities: if self.attributes.is_empty() {
                std::ptr::null_mut()
            } else {
                self.attributes.as_mut_ptr()
            },
            CapabilityCount: self.attributes.len() as u32,
            Reserved: 0,
        }
    }
}

impl Drop for CapabilitySids {
    fn drop(&mut self) {
        for sid in self.owned.drain(..) {
            if !sid.is_null() {
                unsafe {
                    LocalFree(sid);
                }
            }
        }
    }
}

unsafe fn free_sid_array(array: *mut PSID, count: u32) {
    if array.is_null() {
        return;
    }
    for index in 0..count as usize {
        let sid = *array.add(index);
        if !sid.is_null() {
            LocalFree(sid);
        }
    }
    LocalFree(array as *mut c_void);
}

pub fn random_profile_name() -> Result<String> {
    let mut bytes = [0_u8; 16];
    let status = unsafe {
        BCryptGenRandom(
            std::ptr::null_mut(),
            bytes.as_mut_ptr(),
            bytes.len() as u32,
            BCRYPT_USE_SYSTEM_PREFERRED_RNG,
        )
    };
    if status < 0 {
        return Err(anyhow!(
            "BCryptGenRandom failed with NTSTATUS 0x{status:08x}"
        ));
    }
    let suffix: String = bytes.iter().map(|byte| format!("{byte:02x}")).collect();
    Ok(format!("{PROFILE_PREFIX}{suffix}"))
}

pub fn validate_profile_name(name: &str) -> Result<()> {
    if !name.starts_with(PROFILE_PREFIX)
        || name.len() != PROFILE_PREFIX.len() + 32
        || !name[PROFILE_PREFIX.len()..]
            .bytes()
            .all(|value| value.is_ascii_hexdigit())
    {
        return Err(anyhow!("invalid Bello AppContainer profile name"));
    }
    Ok(())
}

pub fn create_profile(name: &str, capabilities: &CapabilitySids) -> Result<AppContainerSid> {
    validate_profile_name(name)?;
    let encoded = wide(name);
    let mut sid: PSID = std::ptr::null_mut();
    let hr = unsafe {
        CreateAppContainerProfile(
            encoded.as_ptr(),
            encoded.as_ptr(),
            encoded.as_ptr(),
            capabilities.profile_slice(),
            capabilities.attributes.len() as u32,
            &mut sid,
        )
    };
    if hr < 0 || sid.is_null() {
        return Err(anyhow!(
            "CreateAppContainerProfile failed with HRESULT 0x{:08x}",
            hr as u32
        ));
    }
    Ok(AppContainerSid(sid))
}

pub fn derive_profile_sid(name: &str) -> Result<AppContainerSid> {
    validate_profile_name(name)?;
    let mut sid: PSID = std::ptr::null_mut();
    let hr = unsafe { DeriveAppContainerSidFromAppContainerName(wide(name).as_ptr(), &mut sid) };
    if hr < 0 || sid.is_null() {
        return Err(anyhow!(
            "DeriveAppContainerSidFromAppContainerName failed with HRESULT 0x{:08x}",
            hr as u32
        ));
    }
    Ok(AppContainerSid(sid))
}

pub fn delete_profile(name: &str) -> Result<()> {
    validate_profile_name(name)?;
    let hr = unsafe { DeleteAppContainerProfile(wide(name).as_ptr()) };
    // Recovery is intentionally idempotent. A journal can exist even when
    // profile creation failed, or survive after profile deletion succeeded.
    const HRESULT_FILE_NOT_FOUND: u32 = 0x8007_0002;
    const HRESULT_NOT_FOUND: u32 = 0x8007_0490;
    if hr < 0 && !matches!(hr as u32, HRESULT_FILE_NOT_FOUND | HRESULT_NOT_FOUND) {
        return Err(anyhow!(
            "DeleteAppContainerProfile failed with HRESULT 0x{:08x}",
            hr as u32
        ));
    }
    Ok(())
}

pub fn sid_string(sid: PSID) -> Result<String> {
    let mut string_ptr: *mut u16 = std::ptr::null_mut();
    if unsafe { ConvertSidToStringSidW(sid, &mut string_ptr) } == 0 {
        return Err(last_error("ConvertSidToStringSidW"));
    }
    let value = unsafe { wide_ptr_to_os_string(string_ptr) }
        .into_string()
        .map_err(|_| anyhow!("AppContainer SID string is not valid Unicode"));
    unsafe {
        LocalFree(string_ptr as *mut c_void);
    }
    value
}

pub fn profile_local_app_data(sid: PSID) -> Result<PathBuf> {
    let sid = sid_string(sid)?;
    let mut path_ptr: *mut u16 = std::ptr::null_mut();
    let hr = unsafe { GetAppContainerFolderPath(wide(sid).as_ptr(), &mut path_ptr) };
    if hr < 0 || path_ptr.is_null() {
        return Err(anyhow!(
            "GetAppContainerFolderPath failed with HRESULT 0x{:08x}",
            hr as u32
        ));
    }
    let path = PathBuf::from(unsafe { wide_ptr_to_os_string(path_ptr) });
    unsafe {
        CoTaskMemFree(path_ptr as *const c_void);
    }
    std::fs::create_dir_all(path.join("Temp")).with_context(|| {
        format!(
            "could not create AppContainer temp under {}",
            path.display()
        )
    })?;
    Ok(path)
}
