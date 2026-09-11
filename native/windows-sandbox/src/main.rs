mod protocol;

use anyhow::{anyhow, Context, Result};
use protocol::{Request, TerminalRecord, MAX_REQUEST_BYTES, PROTOCOL_VERSION};
use std::io::{self, Read, Write};

#[cfg(windows)]
mod acl;
#[cfg(all(windows, test))]
mod acl_lock;
#[cfg(windows)]
mod identity;
#[cfg(windows)]
mod journal;
#[cfg(windows)]
mod process;
#[cfg(windows)]
mod windows;
#[cfg(windows)]
mod winutil;

fn read_request() -> Result<Request> {
    let mut input = io::stdin().lock();
    let mut size_bytes = [0_u8; 4];
    input
        .read_exact(&mut size_bytes)
        .context("request is missing its 4-byte length prefix")?;
    let size = u32::from_le_bytes(size_bytes) as usize;
    if size == 0 || size > MAX_REQUEST_BYTES {
        return Err(anyhow!(
            "request length {size} is outside the supported 1..={MAX_REQUEST_BYTES} range"
        ));
    }
    let mut body = vec![0_u8; size];
    input
        .read_exact(&mut body)
        .context("request frame ended before its declared length")?;
    let request: Request =
        serde_json::from_slice(&body).context("request is not valid protocol JSON")?;
    if request.protocol_version() != PROTOCOL_VERSION {
        return Err(anyhow!(
            "unsupported protocolVersion {}; expected {PROTOCOL_VERSION}",
            request.protocol_version()
        ));
    }
    Ok(request)
}

fn write_record(record: &TerminalRecord<'_>) -> Result<()> {
    let stderr = io::stderr();
    let mut writer = stderr.lock();
    serde_json::to_writer(&mut writer, record).context("could not serialize terminal record")?;
    writer.write_all(b"\n")?;
    writer.flush()?;
    Ok(())
}

#[cfg(windows)]
fn execute(request: Request) -> Result<i32> {
    windows::execute(request)
}

#[cfg(not(windows))]
fn execute(_request: Request) -> Result<i32> {
    Err(anyhow!("bello-windows-sandbox can run only on Windows"))
}

fn real_main() -> i32 {
    std::panic::set_hook(Box::new(|_| {}));
    match std::panic::catch_unwind(|| read_request().and_then(execute)) {
        Ok(Ok(exit_code)) => {
            if write_record(&TerminalRecord::Exit {
                protocol_version: PROTOCOL_VERSION,
                exit_code,
            })
            .is_ok()
            {
                0
            } else {
                3
            }
        }
        Ok(Err(error)) => {
            let message = format!("{error:#}");
            let _ = write_record(&TerminalRecord::Error {
                protocol_version: PROTOCOL_VERSION,
                code: "sandbox_backend_error",
                message: &message,
            });
            2
        }
        Err(_) => {
            let _ = write_record(&TerminalRecord::Error {
                protocol_version: PROTOCOL_VERSION,
                code: "sandbox_backend_panic",
                message: "the native sandbox helper aborted unexpectedly",
            });
            4
        }
    }
}

fn main() {
    std::process::exit(real_main());
}
