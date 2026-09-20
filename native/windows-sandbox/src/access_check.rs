//! Ask Windows about the actual child's effective file permissions.
//!
//! ACL entries for one SID are not an access check: ownership, capabilities,
//! inherited entries, and the AppContainer restriction also affect the result.

use crate::winutil::{last_error, Handle};
use anyhow::{anyhow, Result};
use std::ffi::c_void;
use std::mem;
use windows_sys::Win32::Foundation::{GetLastError, ERROR_INSUFFICIENT_BUFFER};
use windows_sys::Win32::Security::{
    AccessCheck, DuplicateTokenEx, GetKernelObjectSecurity, SecurityIdentification,
    TokenImpersonation, DACL_SECURITY_INFORMATION, GENERIC_MAPPING, GROUP_SECURITY_INFORMATION,
    LABEL_SECURITY_INFORMATION, OWNER_SECURITY_INFORMATION, PRIVILEGE_SET, TOKEN_QUERY,
};
use windows_sys::Win32::Storage::FileSystem::{
    FILE_ALL_ACCESS, FILE_GENERIC_EXECUTE, FILE_GENERIC_READ, FILE_GENERIC_WRITE,
};
use windows_sys::Win32::System::SystemServices::MAXIMUM_ALLOWED;

pub struct AccessVerifier {
    token: Handle,
}

impl AccessVerifier {
    /// `token` must be the already verified, still-suspended child's token,
    /// opened with TOKEN_QUERY | TOKEN_DUPLICATE. Identity verification remains
    /// the launcher's responsibility; do not pass the controller's own token.
    pub fn from_token(token: &Handle) -> Result<Self> {
        let mut duplicate = 0;
        if unsafe {
            DuplicateTokenEx(
                token.raw(),
                TOKEN_QUERY,
                std::ptr::null(),
                SecurityIdentification,
                TokenImpersonation,
                &mut duplicate,
            )
        } == 0
        {
            return Err(last_error("DuplicateTokenEx(access verification)"));
        }
        Ok(Self {
            token: Handle::new(duplicate, "DuplicateTokenEx(access verification)")?,
        })
    }

    /// The caller must already have validated and pinned this file object.
    /// Returns the maximum effective file rights, not merely matching ACE masks.
    /// A failed Windows API call is an error, never evidence that access is denied.
    pub fn granted_file_access(&self, object: &Handle) -> Result<u32> {
        let descriptor = file_security_descriptor(object)?;
        self.granted_access(descriptor.as_ptr() as *mut c_void)
    }

    fn granted_access(&self, descriptor: *mut c_void) -> Result<u32> {
        let mapping = GENERIC_MAPPING {
            GenericRead: FILE_GENERIC_READ,
            GenericWrite: FILE_GENERIC_WRITE,
            GenericExecute: FILE_GENERIC_EXECUTE,
            GenericAll: FILE_ALL_ACCESS,
        };
        let mut required = mem::size_of::<PRIVILEGE_SET>() as u32;
        for _ in 0..3 {
            if required == 0 || required > 64 * 1024 {
                return Err(anyhow!(
                    "AccessCheck returned invalid privilege buffer size {required}"
                ));
            }
            let mut privileges =
                vec![0_usize; (required as usize).div_ceil(mem::size_of::<usize>())];
            let mut size = (privileges.len() * mem::size_of::<usize>()) as u32;
            let mut granted = 0;
            let mut allowed = 0;
            if unsafe {
                AccessCheck(
                    descriptor,
                    self.token.raw(),
                    MAXIMUM_ALLOWED,
                    &mapping,
                    privileges.as_mut_ptr() as *mut PRIVILEGE_SET,
                    &mut size,
                    &mut granted,
                    &mut allowed,
                )
            } != 0
            {
                return Ok(if allowed != 0 { granted } else { 0 });
            }
            let code = unsafe { GetLastError() };
            if code != ERROR_INSUFFICIENT_BUFFER || size <= required {
                return Err(anyhow!("AccessCheck failed with Win32 error {code}"));
            }
            required = size;
        }
        Err(anyhow!(
            "AccessCheck privilege buffer changed size repeatedly"
        ))
    }
}

fn file_security_descriptor(object: &Handle) -> Result<Vec<usize>> {
    // GetKernelObjectSecurity reads the object's stored descriptor directly.
    // Include owner/group for AccessCheck, and the mandatory integrity label.
    let information = OWNER_SECURITY_INFORMATION
        | GROUP_SECURITY_INFORMATION
        | DACL_SECURITY_INFORMATION
        | LABEL_SECURITY_INFORMATION;
    let mut required = 0;
    let first = unsafe {
        GetKernelObjectSecurity(
            object.raw(),
            information,
            std::ptr::null_mut(),
            0,
            &mut required,
        )
    };
    let first_error = unsafe { GetLastError() };
    if first != 0 || first_error != ERROR_INSUFFICIENT_BUFFER {
        return Err(anyhow!(
            "GetKernelObjectSecurity(size) failed with Win32 error {first_error}"
        ));
    }
    for _ in 0..3 {
        if required == 0 || required > 256 * 1024 {
            return Err(anyhow!(
                "GetKernelObjectSecurity returned invalid descriptor size {required}"
            ));
        }
        let mut descriptor = vec![0_usize; (required as usize).div_ceil(mem::size_of::<usize>())];
        let capacity = (descriptor.len() * mem::size_of::<usize>()) as u32;
        if unsafe {
            GetKernelObjectSecurity(
                object.raw(),
                information,
                descriptor.as_mut_ptr() as *mut c_void,
                capacity,
                &mut required,
            )
        } != 0
        {
            return Ok(descriptor);
        }
        let code = unsafe { GetLastError() };
        if code != ERROR_INSUFFICIENT_BUFFER || required <= capacity {
            return Err(anyhow!(
                "GetKernelObjectSecurity failed with Win32 error {code}"
            ));
        }
    }
    Err(anyhow!("file security descriptor changed size repeatedly"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::identity::{create_profile, delete_profile, random_profile_name, CapabilitySids};
    use crate::winutil::{open_path, wide};
    use windows_sys::Win32::Foundation::{LocalFree, PSID, WAIT_OBJECT_0};
    use windows_sys::Win32::Security::Authorization::{
        ConvertSidToStringSidW, ConvertStringSecurityDescriptorToSecurityDescriptorW,
    };
    use windows_sys::Win32::Security::{
        DeriveCapabilitySidsFromName, FreeSid, GetTokenInformation, TokenUser, TOKEN_DUPLICATE,
        TOKEN_USER,
    };
    use windows_sys::Win32::Storage::FileSystem::{FILE_READ_DATA, WRITE_DAC};
    use windows_sys::Win32::System::SystemInformation::GetSystemDirectoryW;
    use windows_sys::Win32::System::Threading::{
        CreateProcessW, DeleteProcThreadAttributeList, GetCurrentProcess,
        InitializeProcThreadAttributeList, OpenProcessToken, TerminateProcess,
        UpdateProcThreadAttribute, WaitForSingleObject, CREATE_NO_WINDOW, CREATE_SUSPENDED,
        EXTENDED_STARTUPINFO_PRESENT, PROCESS_INFORMATION,
        PROC_THREAD_ATTRIBUTE_ALL_APPLICATION_PACKAGES_POLICY,
        PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES, STARTUPINFOEXW,
    };

    fn sid_string(sid: PSID) -> String {
        let mut value = std::ptr::null_mut();
        assert_ne!(unsafe { ConvertSidToStringSidW(sid, &mut value) }, 0);
        let text = unsafe { crate::winutil::wide_ptr_to_os_string(value) }
            .into_string()
            .unwrap();
        unsafe { LocalFree(value as *mut c_void) };
        text
    }

    fn named_capability(name: &str) -> String {
        let mut groups: *mut PSID = std::ptr::null_mut();
        let mut group_count = 0;
        let mut capabilities: *mut PSID = std::ptr::null_mut();
        let mut count = 0;
        assert_ne!(
            unsafe {
                DeriveCapabilitySidsFromName(
                    wide(name).as_ptr(),
                    &mut groups,
                    &mut group_count,
                    &mut capabilities,
                    &mut count,
                )
            },
            0
        );
        assert!(count > 0);
        let result = sid_string(unsafe { *capabilities });
        unsafe {
            for index in 0..group_count {
                FreeSid(*groups.add(index as usize));
            }
            for index in 0..count {
                FreeSid(*capabilities.add(index as usize));
            }
            LocalFree(groups as *mut c_void);
            LocalFree(capabilities as *mut c_void);
        }
        result
    }

    fn check_sddl(verifier: &AccessVerifier, text: &str) -> u32 {
        let mut descriptor = std::ptr::null_mut();
        assert_ne!(
            unsafe {
                ConvertStringSecurityDescriptorToSecurityDescriptorW(
                    wide(text).as_ptr(),
                    1,
                    &mut descriptor,
                    std::ptr::null_mut(),
                )
            },
            0
        );
        let result = verifier.granted_access(descriptor);
        unsafe { LocalFree(descriptor) };
        result.unwrap()
    }

    struct TestProfile(Option<String>);

    impl TestProfile {
        fn cleanup(&mut self) {
            if let Some(name) = &self.0 {
                delete_profile(name).unwrap();
                self.0 = None;
            }
        }
    }

    impl Drop for TestProfile {
        fn drop(&mut self) {
            if let Some(name) = &self.0 {
                let _ = delete_profile(name);
            }
        }
    }

    struct TestAttributes(*mut c_void);

    impl Drop for TestAttributes {
        fn drop(&mut self) {
            unsafe { DeleteProcThreadAttributeList(self.0) };
        }
    }

    struct SuspendedChild {
        process: Handle,
        _thread: Handle,
        profile: TestProfile,
    }

    impl SuspendedChild {
        fn finish(mut self) {
            assert_ne!(unsafe { TerminateProcess(self.process.raw(), 125) }, 0);
            assert_eq!(
                unsafe { WaitForSingleObject(self.process.raw(), 5000) },
                WAIT_OBJECT_0
            );
            self.profile.cleanup();
        }
    }

    impl Drop for SuspendedChild {
        fn drop(&mut self) {
            unsafe {
                TerminateProcess(self.process.raw(), 125);
                WaitForSingleObject(self.process.raw(), 5000);
            }
        }
    }

    fn suspended_lpac() -> (SuspendedChild, Handle, String) {
        let profile = random_profile_name().unwrap();
        let mut capabilities = CapabilitySids::for_network(false).unwrap();
        let sid = create_profile(&profile, &capabilities).unwrap();
        let profile = TestProfile(Some(profile));
        let sid_text = sid_string(sid.0);
        let security = capabilities.security_capabilities(sid.0);
        let policy = 1_u32;
        let mut size = 0;
        unsafe { InitializeProcThreadAttributeList(std::ptr::null_mut(), 2, 0, &mut size) };
        assert!(size > 0);
        let mut attributes = vec![0_usize; size.div_ceil(mem::size_of::<usize>())];
        let list = attributes.as_mut_ptr() as *mut _;
        assert_ne!(
            unsafe { InitializeProcThreadAttributeList(list, 2, 0, &mut size) },
            0
        );
        let attribute_guard = TestAttributes(list);
        assert_ne!(
            unsafe {
                UpdateProcThreadAttribute(
                    list,
                    0,
                    PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES as usize,
                    &security as *const _ as *const c_void,
                    mem::size_of_val(&security),
                    std::ptr::null_mut(),
                    std::ptr::null(),
                )
            },
            0
        );
        assert_ne!(
            unsafe {
                UpdateProcThreadAttribute(
                    list,
                    0,
                    PROC_THREAD_ATTRIBUTE_ALL_APPLICATION_PACKAGES_POLICY as usize,
                    &policy as *const _ as *const c_void,
                    mem::size_of_val(&policy),
                    std::ptr::null_mut(),
                    std::ptr::null(),
                )
            },
            0
        );
        let mut directory = vec![0_u16; 32768];
        let length = unsafe { GetSystemDirectoryW(directory.as_mut_ptr(), directory.len() as u32) };
        assert!(length > 0 && (length as usize) < directory.len());
        directory.truncate(length as usize);
        directory.extend("\\cmd.exe".encode_utf16());
        directory.push(0);
        let mut startup: STARTUPINFOEXW = unsafe { mem::zeroed() };
        startup.StartupInfo.cb = mem::size_of::<STARTUPINFOEXW>() as u32;
        startup.lpAttributeList = list;
        let mut process: PROCESS_INFORMATION = unsafe { mem::zeroed() };
        let created = unsafe {
            CreateProcessW(
                directory.as_ptr(),
                std::ptr::null_mut(),
                std::ptr::null(),
                std::ptr::null(),
                0,
                CREATE_SUSPENDED | CREATE_NO_WINDOW | EXTENDED_STARTUPINFO_PRESENT,
                std::ptr::null(),
                std::ptr::null(),
                &startup.StartupInfo,
                &mut process,
            )
        };
        let error = unsafe { GetLastError() };
        drop(attribute_guard);
        if created == 0 {
            panic!("CreateProcessW(access-check fixture) failed: {error}");
        }
        let child = SuspendedChild {
            process: Handle(process.hProcess),
            _thread: Handle(process.hThread),
            profile,
        };
        let mut token = 0;
        assert_ne!(
            unsafe {
                OpenProcessToken(
                    child.process.raw(),
                    TOKEN_QUERY | TOKEN_DUPLICATE,
                    &mut token,
                )
            },
            0
        );
        (child, Handle(token), sid_text)
    }

    #[test]
    fn actual_lpac_access_check_accounts_for_package_capabilities_and_ownership() {
        let (child, token, sid) = suspended_lpac();
        let verifier = AccessVerifier::from_token(&token).unwrap();
        let base = "O:SYG:SYD:(A;;FA;;;WD)";
        assert_eq!(check_sddl(&verifier, base), 0);
        assert_eq!(
            check_sddl(&verifier, &format!("{base}(A;;FR;;;{sid})")),
            FILE_GENERIC_READ
        );
        assert_eq!(check_sddl(&verifier, &format!("{base}(A;;FR;;;AC)")), 0);
        assert_eq!(
            check_sddl(&verifier, &format!("{base}(A;;0x1;;;S-1-15-2-2)")),
            FILE_READ_DATA
        );
        for name in ["registryRead", "lpacCom"] {
            let capability = named_capability(name);
            assert_eq!(
                check_sddl(&verifier, &format!("{base}(A;;FR;;;{capability})")),
                FILE_GENERIC_READ
            );
        }
        let metadata = named_capability(crate::identity::SYSTEM_ROOT_METADATA_CAPABILITY);
        assert_eq!(
            check_sddl(&verifier, &format!("{base}(A;;0x120088;;;{metadata})")),
            crate::acl::SYSTEM_ROOT_METADATA_MASK,
            "the actual LPAC token must carry the metadata capability, without content/write rights"
        );
        let mut required = 0;
        unsafe {
            GetTokenInformation(
                token.raw(),
                TokenUser,
                std::ptr::null_mut(),
                0,
                &mut required,
            )
        };
        let mut user = vec![0_usize; (required as usize).div_ceil(mem::size_of::<usize>())];
        assert_ne!(
            unsafe {
                GetTokenInformation(
                    token.raw(),
                    TokenUser,
                    user.as_mut_ptr() as *mut c_void,
                    required,
                    &mut required,
                )
            },
            0
        );
        let owner = sid_string(unsafe { (*(user.as_ptr() as *const TOKEN_USER)).User.Sid });
        let rights = check_sddl(
            &verifier,
            &format!("O:{owner}G:{owner}D:(A;;FA;;;{owner})(A;;FR;;;{sid})"),
        );
        assert_eq!(
            rights & WRITE_DAC,
            0,
            "ordinary file ownership bypassed the AppContainer restriction"
        );
        drop(verifier);
        drop(token);
        child.finish();
    }

    #[test]
    fn invalid_object_or_token_is_an_error_not_a_denied_access_result() {
        // The wrapper does not reinterpret a failed query as an empty grant.
        assert!(AccessVerifier::from_token(&Handle(0)).is_err());
        let mut token = 0;
        assert_ne!(
            unsafe {
                OpenProcessToken(
                    GetCurrentProcess(),
                    TOKEN_QUERY | TOKEN_DUPLICATE,
                    &mut token,
                )
            },
            0
        );
        let verifier = AccessVerifier::from_token(&Handle(token)).unwrap();
        assert!(verifier.granted_file_access(&Handle(0)).is_err());
        let current = std::fs::canonicalize(std::env::current_exe().unwrap()).unwrap();
        let object = open_path(&current, false).unwrap();
        assert_ne!(
            verifier.granted_file_access(&object).unwrap() & FILE_READ_DATA,
            0
        );
    }
}
