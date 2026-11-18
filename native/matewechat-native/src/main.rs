//! matewechat-native: OpenMate's link to WeChat, for the openmate-wechat extension.
//!
//! It speaks JSON lines on stdin/stdout with the extension's Python backend, which owns everything else (the store,
//! the inbox, /agent, 自动代答). Only what those two features need is here: QR sign-in, restoring a saved sign-in,
//! the live message stream (push, sync, heartbeat), text sends (with real @s and quotes), names, and the media of a
//! message (a CDN object decrypted into a file the backend names; a voice note as WAV).
//!
//! The protocol is thisnick/agent-wechat's `wechat-protocol` library (MIT), used through its public API only; the
//! online loop follows that project's daemon: heartbeat at the server's interval (30–180 s), a catch-up drain (scene
//! 6) after each connect, scene 1 on a notification, scene 7 when a heartbeat says so, one renewal attempt when the
//! server rejects the session.
//!
//! Lines in:  {"id":N,"op":"…",…}               a request; the answer is {"id":N,"ok":true,…} or {"id":N,"ok":false,"error":{"kind","message"}}
//!            {"op":"persist_ack","seq":S,"ok":bool}   the backend sealed (or failed to seal) a renewed sign-in
//! Lines out: answers, and events (they have "event"): message, cursor, link, auth_lost (each with the listen's
//!            "gen"), and persist. Credentials go out only in login_finish's answer and in persist events.

mod message;

use anyhow::{anyhow, bail, Context, Result};
use base64::Engine as _;
use serde_json::{json, Value};
use std::collections::{HashMap, VecDeque};
use std::io::{BufRead, Write};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{mpsc, Arc, Mutex};
use std::time::{Duration, Instant};
use wechat_protocol::{
    is_retryable_login_poll_error, is_session_rejected, wire, AccountInterrupted, AccountPersistence, AccountState,
    DeviceProfile, LoginStatus, NativeClient, NativeOptions, NativeRequestNotSubmitted, ServiceRejected,
    TextSendOptions,
};

const PROTOCOL: u32 = 2; // the backend checks it in hello: a program of another protocol is never used
const DEVICE_TYPE: &str = "UnifiedPCLinux x86_64";
const COMPUTER_NAME: &str = "OpenMate";
const PERSIST_WAIT: Duration = Duration::from_secs(30);
const INTENT_KEEP: Duration = Duration::from_secs(600);
const B64: base64::engine::GeneralPurpose = base64::engine::general_purpose::STANDARD;

// ---------------------------------------------------------------- output

struct Out(Mutex<std::io::Stdout>);

impl Out {
    fn line(&self, v: &Value) {
        let mut s = serde_json::to_string(v).unwrap_or_else(|_| "{}".into());
        s.push('\n');
        if let Ok(mut o) = self.0.lock() {
            let _ = o.write_all(s.as_bytes());
            let _ = o.flush();
        }
    }
}

fn log(msg: &str) {
    eprintln!("matewechat-native: {msg}");
}

// ---------------------------------------------------------------- persistence of renewed credentials

/// The protocol library calls this before a renewed sign-in takes effect: the backend must seal it first.
struct Persist {
    out: Arc<Out>,
    seq: AtomicU64,
    waiting: Mutex<HashMap<u64, mpsc::Sender<bool>>>,
    latest: Mutex<Option<(String, Value)>>, // (account, the last state it sealed): login_finish answers with it
}

impl Persist {
    fn ack(&self, seq: u64, ok: bool) {
        if let Some(tx) = self.waiting.lock().ok().and_then(|mut w| w.remove(&seq)) {
            let _ = tx.send(ok);
        }
    }
}

impl AccountPersistence for Persist {
    fn persist(&self, _previous: &AccountState, accepted: &AccountState) -> Result<()> {
        let seq = self.seq.fetch_add(1, Ordering::SeqCst) + 1;
        let (tx, rx) = mpsc::channel();
        self.waiting.lock().map_err(|_| anyhow!("persist lock"))?.insert(seq, tx);
        let state = serde_json::to_value(accepted)?;
        self.out.line(&json!({"event": "persist", "seq": seq, "state": state}));
        let ok = rx.recv_timeout(PERSIST_WAIT).unwrap_or(false);
        if let Ok(mut w) = self.waiting.lock() {
            w.remove(&seq);
        }
        if !ok {
            bail!("the renewed sign-in was not stored")
        }
        if let Ok(mut l) = self.latest.lock() {
            *l = Some((accepted.own_username.clone(), state));
        }
        Ok(())
    }
}

// ---------------------------------------------------------------- state

/// A QR sign-in in progress: a worker polls it until it ends; login_poll only reads what it last saw.
struct Login {
    status: Mutex<(String, String)>, // (status, message)
    state: Mutex<Option<AccountState>>,
    cancel: Arc<AtomicBool>,
}

struct App {
    out: Arc<Out>,
    rt: tokio::runtime::Runtime,
    persist: Arc<Persist>,
    login: Mutex<Option<Arc<Login>>>,
    login_seq: AtomicU64, // each login_start's number: only the newest one's attempt is kept
    client: Mutex<Option<NativeClient>>,
    own: Mutex<String>,
    gen: AtomicU64,
    listening: Mutex<Option<(u64, Arc<AtomicBool>)>>,
    intents: Mutex<VecDeque<(Instant, String, String)>>, // (when, chat, text as sent): our own sends' echoes
    cdn: Mutex<Option<(Instant, wechat_protocol::media::CdnConfig)>>, // the media CDN's route, asked for once an hour
}

impl App {
    fn client(&self) -> Result<NativeClient> {
        self.client.lock().map_err(|_| anyhow!("lock"))?.clone().ok_or_else(|| err("signed_out", "微信还没登录"))
    }
    fn own(&self) -> String {
        self.own.lock().map(|o| o.clone()).unwrap_or_default()
    }
    /// Remember a send before it is submitted, so its echo is known even when it comes before the receipt.
    fn intend(&self, chat: &str, text: &str) {
        if let Ok(mut q) = self.intents.lock() {
            let now = Instant::now();
            q.retain(|(at, _, _)| now.duration_since(*at) < INTENT_KEEP);
            q.push_back((now, chat.to_owned(), text.to_owned()));
        }
    }
    /// A send that surely did not go out leaves no echo to wait for: its own intent (this chat, this text) goes.
    fn forget_intent(&self, chat: &str, text: &str) {
        if let Ok(mut q) = self.intents.lock() {
            if let Some(i) = q.iter().rposition(|(_, c, t)| c == chat && t == text) {
                q.remove(i);
            }
        }
    }
    /// Is this outgoing message the echo of a send made here? (Each intent matches once.)
    fn is_echo(&self, chat: &str, text: &str) -> bool {
        let Ok(mut q) = self.intents.lock() else { return false };
        let now = Instant::now();
        q.retain(|(at, _, _)| now.duration_since(*at) < INTENT_KEEP);
        if let Some(i) = q.iter().position(|(_, c, t)| c == chat && t.trim() == text.trim()) {
            q.remove(i);
            return true;
        }
        false
    }
    fn adopt(&self, state: AccountState) -> Result<String> {
        let own = state.own_username.clone();
        let client = NativeClient::from_account_with_persistence(state, NativeOptions::default(), self.persist.clone())?;
        self.stop_listening(None);
        if let Some(old) = self.client.lock().map_err(|_| anyhow!("lock"))?.replace(client) {
            let _ = self.rt.block_on(old.shutdown());
        }
        *self.own.lock().map_err(|_| anyhow!("lock"))? = own.clone();
        if let Ok(mut c) = self.cdn.lock() {
            *c = None; // another sign-in: its own CDN route
        }
        Ok(own)
    }
    /// Stop the live link (only generation `gen` when given: an old listener's stop never stops a newer one).
    fn stop_listening(&self, gen: Option<u64>) {
        if let Ok(mut l) = self.listening.lock() {
            if let Some((g, stop)) = l.as_ref() {
                if gen.is_none() || gen == Some(*g) {
                    stop.store(true, Ordering::SeqCst);
                    *l = None;
                }
            }
        }
    }
}

/// An error with a kind the backend maps (signed_out, expired, rejected, not_sent, input, lan, unsupported…).
#[derive(Debug)]
struct Kinded {
    kind: &'static str,
    message: String,
}
impl std::fmt::Display for Kinded {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}
impl std::error::Error for Kinded {}
fn err(kind: &'static str, message: &str) -> anyhow::Error {
    anyhow::Error::new(Kinded { kind, message: message.to_owned() })
}
fn kinded(e: &anyhow::Error) -> Option<&Kinded> {
    e.downcast_ref::<Kinded>().or_else(|| e.chain().find_map(|c| c.downcast_ref::<Kinded>()))
}
fn kind_of(e: &anyhow::Error) -> &'static str {
    if let Some(k) = kinded(e) {
        return k.kind;
    }
    if is_session_rejected(e) {
        return "expired";
    }
    "error"
}
fn not_submitted(e: &anyhow::Error) -> bool {
    e.is::<NativeRequestNotSubmitted>() || e.chain().any(|c| c.downcast_ref::<NativeRequestNotSubmitted>().is_some())
}

// ---------------------------------------------------------------- sign-in

fn str_arg<'a>(p: &'a Value, name: &str) -> Result<&'a str> {
    p.get(name).and_then(Value::as_str).filter(|s| !s.is_empty()).ok_or_else(|| err("input", &format!("缺少 {name}")))
}

fn decode_qr(png: &[u8]) -> Result<String> {
    let img = image::load_from_memory(png).context("二维码图片读不出来")?.to_luma8();
    let mut prepared = rqrr::PreparedImage::prepare(img);
    let grids = prepared.detect_grids();
    let grid = grids.first().ok_or_else(|| anyhow!("二维码图片里没有找到二维码"))?;
    let (_, content) = grid.decode().map_err(|e| anyhow!("二维码解不开：{e:?}"))?;
    Ok(content)
}

fn op_login_start(app: &App, p: &Value) -> Result<Value> {
    let device = match p.get("device") {
        Some(v) if !v.is_null() => serde_json::from_value::<DeviceProfile>(v.clone()).context("保存的设备信息读不出来")?,
        _ => DeviceProfile::generate(DEVICE_TYPE.into())?,
    };
    let mine = app.login_seq.fetch_add(1, Ordering::SeqCst) + 1;
    if let Some(old) = app.login.lock().map_err(|_| anyhow!("lock"))?.take() {
        old.cancel.store(true, Ordering::SeqCst);
    }
    let boot = NativeClient::new(NativeOptions::default())?;
    let mut attempt = boot.start_qr_login(device, COMPUTER_NAME)?;
    let url = decode_qr(&attempt.qr_png)?;
    let out = json!({"qr": url, "expires_at": attempt.expires_at, "device": serde_json::to_value(&attempt.device)?});
    let login = Arc::new(Login {
        status: Mutex::new(("pending".into(), String::new())),
        state: Mutex::new(None),
        cancel: Arc::new(AtomicBool::new(false)),
    });
    {
        // a slower earlier start must not replace the attempt of a newer one (whose QR is the one shown)
        let mut slot = app.login.lock().map_err(|_| anyhow!("lock"))?;
        if app.login_seq.load(Ordering::SeqCst) != mine {
            login.cancel.store(true, Ordering::SeqCst);
            return Err(err("input", "这次取的二维码已经被更新的一次取代"));
        }
        if let Some(old) = slot.replace(login.clone()) {
            old.cancel.store(true, Ordering::SeqCst);
        }
    }
    // The worker owns the attempt until it ends: a LAN confirmation the phone makes late still finds its listener.
    std::thread::Builder::new().name("login".into()).spawn(move || {
        let set = |s: &str, m: &str| {
            if let Ok(mut st) = login.status.lock() {
                *st = (s.to_owned(), m.to_owned());
            }
        };
        let deadline = Instant::now() + Duration::from_secs(600);
        loop {
            if login.cancel.load(Ordering::SeqCst) {
                return;
            }
            let cancel = login.cancel.clone();
            match boot.poll_login_guarded(&mut attempt, deadline, move || cancel.load(Ordering::SeqCst)) {
                Ok(LoginStatus::Pending) => set("pending", ""),
                Ok(LoginStatus::Scanned) => set("scanned", ""),
                Ok(LoginStatus::Approved) | Ok(LoginStatus::Completed) => {
                    match boot.complete_login(&mut attempt) {
                        Ok(state) => {
                            if let Ok(mut s) = login.state.lock() {
                                *s = Some(state);
                            }
                            set("confirmed", "");
                        }
                        Err(e) => set("failed", &format!("登录没有完成：{e:#}")),
                    }
                    return;
                }
                Ok(LoginStatus::Expired) => return set("expired", ""),
                Ok(LoginStatus::Cancelled) | Ok(LoginStatus::Rejected) => return set("expired", "手机上取消了登录"),
                Err(e) if is_retryable_login_poll_error(&e) => {}
                Err(e) => {
                    if let Some(reason) = wechat_protocol::login_lan_error_reason(&e) {
                        return set("failed", &format!("微信要求手机和这台电脑在同一个局域网里确认登录（{reason}）"));
                    }
                    return set("failed", &format!("{e:#}"));
                }
            }
            if Instant::now() >= deadline {
                return set("expired", "");
            }
            std::thread::sleep(Duration::from_secs(1));
        }
    })?;
    Ok(out)
}

fn op_login_poll(app: &App) -> Result<Value> {
    let login = app.login.lock().map_err(|_| anyhow!("lock"))?.clone().ok_or_else(|| err("input", "没有进行中的扫码登录"))?;
    let (status, message) = login.status.lock().map_err(|_| anyhow!("lock"))?.clone();
    if status == "failed" {
        return Err(err("login", &message));
    }
    Ok(json!({"status": status, "message": message}))
}

fn account_name(app: &App, client: &NativeClient, own: &str) -> String {
    let lookup = app.rt.block_on(async {
        tokio::time::timeout(Duration::from_secs(5), client.lookup_contacts(&[own.to_owned()])).await
    });
    match lookup {
        Ok(Ok(found)) => found
            .contacts
            .into_iter()
            .find(|c| c.username == own)
            .and_then(|c| c.nickname)
            .filter(|n| !n.is_empty())
            .unwrap_or_else(|| own.to_owned()),
        _ => own.to_owned(),
    }
}

fn op_login_finish(app: &App) -> Result<Value> {
    let login = app.login.lock().map_err(|_| anyhow!("lock"))?.take().ok_or_else(|| err("input", "没有进行中的扫码登录"))?;
    let state = login.state.lock().map_err(|_| anyhow!("lock"))?.take().ok_or_else(|| err("input", "还没有在手机上确认"))?;
    let first = serde_json::to_value(&state)?;
    let own = app.adopt(state)?;
    if let Ok(mut l) = app.persist.latest.lock() {
        *l = None; // after the old account stopped: whatever it sealed late is not this sign-in's
    }
    let name = account_name(app, &app.client()?, &own);
    // a renewal during the name lookup was sealed already: answer with that, never the older first state
    let latest = app
        .persist
        .latest
        .lock()
        .ok()
        .and_then(|l| l.clone())
        .filter(|(who, _)| *who == own)
        .map(|(_, v)| v)
        .unwrap_or(first);
    Ok(json!({"state": latest, "account": {"id": own, "name": name}}))
}

fn op_restore(app: &App, p: &Value) -> Result<Value> {
    let state: AccountState =
        serde_json::from_value(p.get("state").cloned().unwrap_or(Value::Null)).map_err(|_| err("expired", "保存的登录信息读不出来"))?;
    let own = app.adopt(state)?;
    let name = account_name(app, &app.client()?, &own);
    Ok(json!({"account": {"id": own, "name": name}}))
}

fn op_logout(app: &App) -> Result<Value> {
    if let Some(l) = app.login.lock().ok().and_then(|mut l| l.take()) {
        l.cancel.store(true, Ordering::SeqCst);
    }
    app.stop_listening(None);
    let client = app.client.lock().map_err(|_| anyhow!("lock"))?.take();
    if let Some(c) = client {
        let _ = app.rt.block_on(async { tokio::time::timeout(Duration::from_secs(8), c.logout()).await });
        let _ = app.rt.block_on(c.shutdown());
    }
    Ok(json!({}))
}

// ---------------------------------------------------------------- the live link

fn op_listen(app: Arc<App>, p: &Value) -> Result<Value> {
    let client = app.client()?;
    let cursor = match p.get("cursor").and_then(Value::as_str) {
        Some(s) if !s.is_empty() => B64.decode(s).map_err(|_| err("input", "cursor"))?,
        _ => vec![],
    };
    app.stop_listening(None);
    let gen = match p.get("gen").and_then(Value::as_u64) {
        Some(g) if g > 0 => g, // the backend numbers its listens (each has its own queue there)
        _ => app.gen.fetch_add(1, Ordering::SeqCst) + 1,
    };
    let stop = Arc::new(AtomicBool::new(false));
    *app.listening.lock().map_err(|_| anyhow!("lock"))? = Some((gen, stop.clone()));
    std::thread::Builder::new().name("listen".into()).spawn(move || listen_loop(app, client, cursor, stop, gen))?;
    Ok(json!({"gen": gen}))
}

fn listen_loop(app: Arc<App>, client: NativeClient, mut cursor: Vec<u8>, stop: Arc<AtomicBool>, gen: u64) {
    let mut backoff = Duration::from_secs(1);
    let mut renewed = false;
    while !stop.load(Ordering::SeqCst) {
        let started = Instant::now();
        let result = session(&app, &client, &mut cursor, &stop, gen);
        if started.elapsed() > Duration::from_secs(60) {
            renewed = false; // a link that stayed up: the next rejection may be renewed again
            backoff = Duration::from_secs(1);
        }
        app.out.line(&json!({"event": "link", "gen": gen, "up": false}));
        let e = match result {
            Ok(()) => return,
            Err(e) => e,
        };
        if stop.load(Ordering::SeqCst) {
            return;
        }
        match AccountInterrupted::find(&e) {
            Some(AccountInterrupted::Stopped) | Some(AccountInterrupted::Transferred) => return,
            Some(AccountInterrupted::CredentialsSuperseded) => continue,
            _ => {}
        }
        if is_session_rejected(&e) {
            // the daemon's rule: one renewal with the saved credentials before asking for a new QR
            if !renewed {
                renewed = true;
                match client.renew_session_blocking(COMPUTER_NAME) {
                    Ok(()) => {
                        log("session renewed");
                        continue;
                    }
                    Err(r) if is_session_rejected(&r) => log(&format!("renewal rejected: {r:#}")),
                    Err(r) => {
                        // the network, or the renewed sign-in could not be sealed: try again later, not a new QR
                        log(&format!("renewal failed for now: {r:#}"));
                        renewed = false;
                        sleep_unless(&stop, backoff);
                        backoff = (backoff * 2).min(Duration::from_secs(60));
                        continue;
                    }
                }
            }
            app.out.line(&json!({"event": "auth_lost", "gen": gen, "message": "微信登录已失效，需要重新扫码"}));
            return;
        }
        log(&format!("link down: {e:#}"));
        sleep_unless(&stop, backoff);
        backoff = (backoff * 2).min(Duration::from_secs(60));
    }
}

fn sleep_unless(stop: &AtomicBool, d: Duration) {
    let until = Instant::now() + d;
    while Instant::now() < until && !stop.load(Ordering::SeqCst) {
        std::thread::sleep(Duration::from_millis(200));
    }
}

fn heartbeat_interval(next: u32) -> Duration {
    Duration::from_secs((next as u64).clamp(30, 180))
}

fn session(app: &App, client: &NativeClient, cursor: &mut Vec<u8>, stop: &AtomicBool, gen: u64) -> Result<()> {
    let mut conn = client.open_push_connection_blocking()?;
    let beat = conn.heartbeat(cursor)?;
    drain(app, &mut conn, cursor, 6, "catchup", gen, stop)?; // what came while away: stored, never acted on
    app.out.line(&json!({"event": "link", "gen": gen, "up": true}));
    let mut next_beat = Instant::now() + heartbeat_interval(beat.next_interval);
    loop {
        if stop.load(Ordering::SeqCst) {
            return Ok(());
        }
        if Instant::now() >= next_beat {
            let beat = conn.heartbeat(cursor)?;
            next_beat = Instant::now() + heartbeat_interval(beat.next_interval);
            if beat.selector & 7 != 0 {
                drain(app, &mut conn, cursor, 7, "live", gen, stop)?;
            }
        }
        if let Some(n) = conn.poll(Duration::from_secs(1))? {
            if n.command == 356 {
                // a contact change (a group made, joined or renamed among them): the backend reads its group list again
                app.out.line(&json!({"event": "contacts", "gen": gen}));
            } else {
                drain(app, &mut conn, cursor, 1, "live", gen, stop)?;
            }
        }
    }
}

/// A sync command this program does not read: named in the log once per run (what WeChat sends, for later work).
fn note_command(id: i32) {
    static SEEN: std::sync::OnceLock<Mutex<std::collections::HashSet<i32>>> = std::sync::OnceLock::new();
    if SEEN.get_or_init(|| Mutex::new(Default::default())).lock().map(|mut s| s.insert(id)).unwrap_or(false) {
        log(&format!("sync command {id} seen (not read)"));
    }
}

/// Sync until the server says there is no more. After a reset (-2006) everything up to the first normal last page
/// from the new cursor is catch-up (history the server sends again, never a request); a reset on that page goes on.
fn drain(
    app: &App,
    conn: &mut wechat_protocol::AccountPushConnection,
    cursor: &mut Vec<u8>,
    scene: u32,
    origin: &str,
    gen: u64,
    stop: &AtomicBool,
) -> Result<()> {
    let own = app.own();
    let mut origin = origin;
    // normal pages that move the cursor go on as long as the server has more (they stay live); only resets in a
    // row, or pages that do not move the cursor, end it with an error (the link reconnects)
    let (mut resets, mut stuck) = (0u32, 0u32);
    for _ in 0..100_000 {
        let had_cursor = !cursor.is_empty();
        let before = cursor.clone();
        let batch = conn.sync(cursor, scene)?;
        let reset = batch.code == -2006 || (had_cursor && batch.cursor.is_empty());
        resets = if reset { resets + 1 } else { 0 };
        if resets > 16 {
            bail!("too many sync resets in a row");
        }
        if reset {
            origin = "catchup"; // the server reset the cursor: what it sends again is history, never a request
        }
        if stop.load(Ordering::SeqCst) {
            return Ok(()); // a stopped listener says nothing more
        }
        for command in &batch.commands {
            if command.id == 4 {
                // DelContact: a contact or a group gone from the account (a group quit or deleted on the phone)
                match message::deleted_contact(&command.payload) {
                    Ok(id) => app.out.line(&json!({"event": "removed", "gen": gen, "id": id, "origin": origin})),
                    Err(e) => log(&format!("a contact removal could not be read: {e:#}")),
                }
                continue;
            }
            if command.id != 5 {
                note_command(command.id);
                continue;
            }
            let m = match wire::decode_add_message(&command.payload, &own) {
                Ok(m) => m,
                Err(e) => {
                    log(&format!("a message could not be decoded: {e:#}"));
                    continue;
                }
            };
            let server_id = m.server_message_id.clone().unwrap_or_default();
            let content = m.content_text.clone().unwrap_or_else(|| String::from_utf8_lossy(&m.content).into_owned());
            let source = m.msgsource.as_ref().map(|s| String::from_utf8_lossy(s).into_owned());
            let raw = message::Raw {
                chat: &m.peer,
                from: &m.from,
                outgoing: m.outgoing,
                server_id: &server_id,
                message_type: m.message_type,
                content: &content,
                created_at: m.created_at,
                msgsource: source.as_deref(),
            };
            if let Some((chat, at)) = message::read_control(&raw, &own) {
                // read on the phone (or another device): the backend counts the chat as handled up to then
                app.out.line(&json!({"event": "read", "gen": gen, "chat": chat, "at": at, "origin": origin}));
                continue;
            }
            if raw.message_type == 51 {
                continue; // the other status notices (a chat opened, a device handing over): never a message to show
            }
            let (mut event, _) = message::normalize(&raw, &own, origin);
            if let Some(data) = message::inline_media(m.message_type, &m.inline_data) {
                event["inline"] = json!(B64.encode(data)); // a voice note's SILK, or a picture's thumbnail
            }
            if m.outgoing {
                let text = event.get("text").and_then(Value::as_str).unwrap_or("").to_owned();
                if app.is_echo(&m.peer, &text) {
                    event["origin"] = json!("local_send");
                }
            }
            event["gen"] = json!(gen);
            app.out.line(&event);
        }
        // the protocol computes the next cursor (an empty one is a reset the server asked for)
        *cursor = batch.cursor.clone();
        // after the batch's messages: the backend stores the cursor once it has handled them
        app.out.line(&json!({"event": "cursor", "gen": gen, "cursor": B64.encode(&*cursor)}));
        if !batch.continuation && !reset {
            return Ok(()); // a normal last page: the next drain is live again
        }
        stuck = if !reset && *cursor == before { stuck + 1 } else { 0 };
        if stuck >= 3 {
            bail!("the sync cursor does not move");
        }
    }
    bail!("too many sync pages")
}

// ---------------------------------------------------------------- sends

/// agent-wechat's daemon: from_user is me for my own message, the group for a group member's, else the sender;
/// chat_user is the sender.
fn quote_reference(q: &Value, chat: &str, own: &str) -> Result<wechat_protocol::quote_send::QuoteReference> {
    let get = |k: &str| q.get(k).and_then(Value::as_str).unwrap_or("").to_owned();
    let outgoing = q.get("outgoing").and_then(Value::as_bool).unwrap_or(false);
    let sender = if outgoing { own.to_owned() } else { get("sender") };
    if get("server_id").is_empty() || sender.is_empty() || get("content").is_empty() {
        return Err(err("input", "引用的消息缺少信息"));
    }
    let from_user = if outgoing {
        own.to_owned()
    } else if message::is_group(chat) {
        chat.to_owned()
    } else {
        sender.clone()
    };
    let display = get("display_name");
    Ok(wechat_protocol::quote_send::QuoteReference {
        message_type: 1,
        server_id: get("server_id"),
        from_user,
        chat_user: sender.clone(),
        display_name: if display.is_empty() { sender } else { display },
        content: get("content"),
        created_at: q.get("created_at").and_then(Value::as_u64).unwrap_or(0) as u32,
        msg_source: Some(get("msgsource")).filter(|s| !s.is_empty()),
    })
}

/// How a send ended: "sent" (the server took this very send), "rejected" (the server refused it), "not_sent"
/// (nothing left this program) or "uncertain" (it may have gone out: never sent again blindly).
fn send_outcome(e: &anyhow::Error) -> (&'static str, String) {
    if not_submitted(e) {
        return ("not_sent", format!("{e:#}"));
    }
    if let Some(k) = kinded(e) {
        return (if k.kind == "rejected" { "rejected" } else { "not_sent" }, k.message.clone());
    }
    if let Some(r) = ServiceRejected::find(e) {
        return ("rejected", format!("微信拒绝了这条消息（{}）", r.code));
    }
    ("uncertain", format!("{e:#}"))
}

fn op_send(app: &App, p: &Value) -> Result<Value> {
    let chat = str_arg(p, "chat")?.to_owned();
    let text = str_arg(p, "text")?.to_owned();
    let key = str_arg(p, "key")?.to_owned();
    let quote = p.get("quote").filter(|q| q.is_object()).cloned();
    let mut members: Vec<(String, String)> = vec![];
    for m in p.get("mentions").and_then(Value::as_array).cloned().unwrap_or_default() {
        let id = m.get("id").and_then(Value::as_str).unwrap_or("").to_owned();
        let name = m.get("name").and_then(Value::as_str).unwrap_or("").to_owned();
        if !id.is_empty() {
            members.push((id, name));
        }
    }
    let client = app.client()?;
    let own = app.own();
    let intended: std::cell::RefCell<Option<String>> = std::cell::RefCell::new(None); // the text this send registered
    let sent_at = std::cell::Cell::new(0u32); // when this send was made (the time the receipt belongs to)
    let attempt = (|| -> Result<(String, Option<String>)> {
        if text.trim().is_empty() || text.len() > 16 * 1024 || text.contains('\0') {
            return Err(err("input", "消息内容是空的或太长"));
        }
        if !members.is_empty() && !message::is_group(&chat) {
            return Err(err("input", "只有群里能 @ 人"));
        }
        let (rendered, source) = message::render_mentions(&text, &members).map_err(|e| err("input", &e.to_string()))?;
        let client_id = wire::client_message_id(key.as_bytes());
        if client_id == 0 {
            return Err(err("input", "key"));
        }
        let created_at = wire::unix_time()?;
        sent_at.set(created_at);
        if let Some(q) = &quote {
            let reference = quote_reference(q, &chat, &own)?;
            let xml = wechat_protocol::quote_send::quote_xml(&own, &rendered, &reference)
                .map_err(|e| err("input", &format!("引用消息组不出来：{e}")))?;
            app.intend(&chat, &rendered);
            *intended.borrow_mut() = Some(rendered.clone());
            let r = app.rt.block_on(client.send_quote(&chat, &client_id.to_string(), created_at, &xml, source.as_deref()))?;
            return match r.status {
                wechat_protocol::media_send::SendStatus::ServerAccepted if r.client_id_matches == Some(true) => {
                    Ok(("sent".into(), r.server_message_id))
                }
                wechat_protocol::media_send::SendStatus::Rejected => {
                    Err(err("rejected", &format!("微信拒绝了这条消息（{}）", r.service_code)))
                }
                _ => Ok(("uncertain".into(), None)), // accepted without matching this send, or needs review
            };
        }
        app.intend(&chat, &rendered);
        *intended.borrow_mut() = Some(rendered.clone());
        let r = app.rt.block_on(client.send_text(
            &chat,
            &rendered,
            TextSendOptions { created_at, client_message_id: client_id, source },
        ))?;
        if !r.accepted && r.outer_code != 0 {
            // the whole request was refused (no item came back to match): nothing went out
            return Err(err("rejected", &format!("微信拒绝了这条消息（{}）", r.outer_code)));
        }
        let matches = r.returned_destination_matches && r.returned_client_id_matches;
        if !matches {
            return Ok(("uncertain".into(), None)); // a receipt that is not this send's says nothing about it
        }
        if !r.accepted {
            let code = r.item_code.unwrap_or(r.outer_code);
            return Err(err("rejected", &format!("微信拒绝了这条消息（{code}）")));
        }
        Ok(("sent".into(), r.server_message_id))
    })();
    match attempt {
        Ok((status, id)) => Ok(json!({"status": status, "message_id": id.unwrap_or_default(), "quoted": quote.is_some(),
                                      "created_at": sent_at.get()})),
        Err(e) => {
            let (status, why) = send_outcome(&e);
            if status != "uncertain" {
                if let Some(text) = intended.borrow().as_deref() {
                    app.forget_intent(&chat, text); // nothing will echo
                }
            }
            log(&format!("send {status}: {why}"));
            Ok(json!({"status": status, "message": why}))
        }
    }
}

// ---------------------------------------------------------------- names

fn op_contacts(app: &App, p: &Value) -> Result<Value> {
    let client = app.client()?;
    let ids: Vec<String> = p
        .get("ids")
        .and_then(Value::as_array)
        .map(|a| a.iter().filter_map(Value::as_str).filter(|s| !s.is_empty()).map(str::to_owned).take(40).collect())
        .unwrap_or_default();
    // a WeCom contact (…@openim) is read with its own request, one at a time; the rest two per request
    let (openim, plain): (Vec<String>, Vec<String>) = ids.into_iter().partition(|i| i.ends_with("@openim"));
    let mut found = vec![];
    // ids whose request failed: not asked yet (the protocol answers an id WeChat says it cannot show as a success
    // with no record, so every error here is a failure)
    let mut failed: Vec<String> = vec![];
    for pair in plain.chunks(2) {
        match lookup_contacts(app, &client, pair) {
            Ok(cs) => found.extend(cs),
            Err(e) if pair.len() == 2 => {
                // WeChat answers an id it can't show with an empty record, failing the pair: each on its own then
                log(&format!("contact lookup failed: {e}; trying each on its own"));
                for one in pair {
                    match lookup_contacts(app, &client, std::slice::from_ref(one)) {
                        Ok(cs) => found.extend(cs),
                        Err(e) => {
                            log(&format!("contact lookup failed for one: {e}"));
                            failed.push(one.clone());
                        }
                    }
                }
            }
            Err(e) => {
                log(&format!("contact lookup failed: {e}"));
                failed.extend(pair.iter().cloned());
            }
        }
    }
    for id in openim {
        let (c, who) = (client.clone(), id.clone());
        let r = app.rt.block_on(async {
            tokio::time::timeout(
                Duration::from_secs(8),
                tokio::task::spawn_blocking(move || c.request_blocking(|r| r.lookup_openim_contact(&who, None, None))),
            )
            .await
        });
        match r {
            Ok(Ok(Ok(c))) if c.username == id => {
                found.push(json!({"id": c.username, "nickname": c.nickname, "remark": c.remark, "alias": null,
                                  "verify_flag": null, "business": false}));
            }
            Ok(Ok(Ok(_))) => log("WeCom contact lookup answered another id"),
            Ok(Ok(Err(e))) => log(&format!("WeCom contact lookup failed: {e:#}")),
            Ok(Err(_)) => {
                log("WeCom contact lookup stopped");
                failed.push(id.clone());
            }
            Err(_) => {
                log("WeCom contact lookup timed out");
                failed.push(id.clone());
            }
        }
    }
    Ok(json!({"contacts": found, "failed": failed}))
}

/// One or two ordinary contacts: their records, or why not.
fn lookup_contacts(app: &App, client: &NativeClient, ids: &[String]) -> std::result::Result<Vec<Value>, String> {
    let r = app.rt.block_on(async { tokio::time::timeout(Duration::from_secs(8), client.lookup_contacts(ids)).await });
    match r {
        Ok(Ok(lookup)) => Ok(lookup
            .contacts
            .into_iter()
            .map(|c| {
                // field 24 of the contact is its verify flag: bit 8 marks a verified official / service account
                let flag = wechat_protocol::protobuf::decode(&c.raw)
                    .ok()
                    .and_then(|f| match wechat_protocol::protobuf::optional(&f, 24) {
                        Ok(Some(wechat_protocol::protobuf::Value::Varint(v))) => Some(*v),
                        _ => None,
                    });
                json!({"id": c.username, "nickname": c.nickname, "remark": c.remark, "alias": c.alias,
                       "verify_flag": flag, "business": flag.is_some_and(|v| v & 8 != 0)})
            })
            .collect()),
        Ok(Err(e)) => Err(format!("{e:#}")),
        Err(_) => Err("timed out".into()),
    }
}

fn op_room(app: &App, p: &Value) -> Result<Value> {
    let client = app.client()?;
    let room = str_arg(p, "room_id")?.to_owned();
    if !message::is_group(&room) {
        return Err(err("input", "不是群"));
    }
    if !room.ends_with("@chatroom") {
        return Err(err("unsupported", "企业微信的群读不到成员名单"));
    }
    let r = app.rt.block_on(async { tokio::time::timeout(Duration::from_secs(8), client.lookup_room(&room, 0)).await });
    let roster = match r {
        Ok(Ok(r)) => r,
        Ok(Err(e)) => return Err(e),
        Err(_) => return Err(anyhow!("查群成员超时")),
    };
    let members: Vec<Value> = roster
        .members
        .iter()
        .map(|m| json!({"id": m.username, "nickname": m.nickname, "display": m.room_display_name}))
        .collect();
    let count = roster.full_member_ids.as_ref().map(|ids| ids.len()).unwrap_or(0);
    // whether the account is still in it, from the full id list only (the member records may be a part): null
    // when WeChat gave no full list
    let own = app.own();
    let me_in = roster.full_member_ids.as_ref().filter(|_| !own.is_empty()).map(|ids| ids.iter().any(|i| *i == own));
    Ok(json!({"room_id": roster.room_id, "members": members, "member_count": count, "owner": roster.owner,
              "me_in": me_in}))
}

/// Every group in the account's contacts (the 通讯录 list WeChat pages out at sign-in); groups never saved there show
/// up only once they have a message.
fn op_groups(app: &App) -> Result<Value> {
    let client = app.client()?;
    let (mut contacts, mut rooms) = (0u32, 0u32);
    let mut groups = std::collections::BTreeSet::new();
    let deadline = Instant::now() + Duration::from_secs(150); // within the backend's wait: never a page after it gave up
    let mut complete = false;
    let mut total = 0usize; // every contact the pages named (friends, groups, official accounts)
    for _ in 0..200 {
        if Instant::now() >= deadline {
            break;
        }
        let page = app
            .rt
            .block_on(async { tokio::time::timeout(Duration::from_secs(20), client.init_contacts(contacts, rooms)).await })
            .map_err(|_| anyhow!("读通讯录超时"))??;
        total += page.usernames.len();
        for u in page.usernames {
            if message::is_group(&u) {
                groups.insert(u);
            }
        }
        let moved = page.contact_sequence != contacts || page.room_sequence != rooms;
        contacts = page.contact_sequence;
        rooms = page.room_sequence;
        if !page.more {
            complete = true;
            break;
        }
        if !moved {
            break;
        }
    }
    Ok(json!({"groups": groups.into_iter().collect::<Vec<_>>(), "complete": complete, "contacts": total}))
}

// ---------------------------------------------------------------- media

const CDN_KEEP: Duration = Duration::from_secs(3600);
const MEDIA_MOST: u64 = wechat_protocol::media::MAX_CDN_TRANSFER_BYTES as u64; // 25 MB: one ordinary CDN transfer

/// The CDN route (asked of WeChat once an hour, or again after a failed download).
fn cdn_config(app: &App, client: &NativeClient, fresh: bool) -> Result<wechat_protocol::media::CdnConfig> {
    if !fresh {
        if let Some((at, c)) = app.cdn.lock().map_err(|_| anyhow!("lock"))?.as_ref() {
            if at.elapsed() < CDN_KEEP {
                return Ok(c.clone());
            }
        }
    }
    let c = app
        .rt
        .block_on(async { tokio::time::timeout(Duration::from_secs(15), client.cdn_config()).await })
        .map_err(|_| err("network", "问微信要媒体服务器地址超时"))??;
    *app.cdn.lock().map_err(|_| anyhow!("lock"))? = Some((Instant::now(), c.clone()));
    Ok(c)
}

/// A file the backend named: absolute, in an existing directory; written through a temporary file beside it.
fn target_path(p: &Value) -> Result<std::path::PathBuf> {
    let path = std::path::PathBuf::from(str_arg(p, "path")?);
    if !path.is_absolute() || path.file_name().is_none() || !path.parent().is_some_and(|d| d.is_dir()) {
        return Err(err("input", "保存位置不对"));
    }
    Ok(path)
}

fn write_file(path: &std::path::Path, bytes: &[u8]) -> Result<()> {
    let mut tmp = path.as_os_str().to_owned();
    tmp.push(".part");
    let tmp = std::path::PathBuf::from(tmp);
    std::fs::write(&tmp, bytes).context("媒体文件写不进去")?;
    std::fs::rename(&tmp, path).inspect_err(|_| {
        let _ = std::fs::remove_file(&tmp);
    })?;
    Ok(())
}

/// Why a download failed, in words for people (never the file id or the key).
fn media_error(e: &anyhow::Error) -> anyhow::Error {
    if kinded(e).is_some() {
        return err(kind_of(e), &kinded(e).map(|k| k.message.clone()).unwrap_or_default());
    }
    let text = format!("{e:#}");
    if text.contains("media_expired") {
        return err("expired_media", "文件已过期，微信服务器上取不到了");
    }
    if let Some(r) = ServiceRejected::find(e) {
        return err("rejected", &format!("微信服务器没有给这个文件（{}），可能已过期或被撤回", r.code));
    }
    if is_session_rejected(e) {
        return err("expired", "微信登录已失效");
    }
    if text.contains("outside limit") || text.contains("too large") {
        return err("too_large", "文件太大");
    }
    if text.contains("checksum") || text.contains("integrity") || text.contains("mismatch") {
        return err("integrity", "下载的文件校验没通过");
    }
    err("network", "下载没成功（网络或微信服务器的问题）")
}

/// Download one CDN object of a media message, decrypt it and write it to "path".
/// In: quality (original | standard | thumbnail | video | file), file_id, aes_key (32 hex), size (advertised, optional),
/// most (bytes, at most 25 MB), path. Out: bytes, md5, picture_format.
fn op_media(app: &App, p: &Value) -> Result<Value> {
    use wechat_protocol::media::{CdnClient, MediaDescriptor, MediaQuality};
    let client = app.client()?;
    let quality = match str_arg(p, "quality")? {
        "original" => MediaQuality::Original,
        "standard" => MediaQuality::Standard,
        "thumbnail" => MediaQuality::Thumbnail,
        "video" => MediaQuality::Video,
        "file" => MediaQuality::File,
        _ => return Err(err("input", "quality")),
    };
    let file_id = str_arg(p, "file_id")?.to_owned();
    let key_hex = str_arg(p, "aes_key")?;
    if key_hex.len() != 32 {
        return Err(err("input", "aes_key"));
    }
    let aes_key = (0..16)
        .map(|i| u8::from_str_radix(&key_hex[i * 2..i * 2 + 2], 16))
        .collect::<std::result::Result<Vec<u8>, _>>()
        .map_err(|_| err("input", "aes_key"))?;
    let most = p.get("most").and_then(Value::as_u64).unwrap_or(MEDIA_MOST).min(MEDIA_MOST);
    let size = p.get("size").and_then(Value::as_u64).filter(|n| *n > 0);
    if size.is_some_and(|n| n > most) {
        return Err(err("too_large", "文件太大"));
    }
    let path = target_path(p)?;
    let descriptor = MediaDescriptor {
        file_id,
        aes_key,
        quality,
        // a picture's advertised length is the original's, not the size asked for: not compared
        expected_bytes: if matches!(quality, MediaQuality::Video | MediaQuality::File) { size } else { None },
        expected_md5: None,
        strict_size: false,
        expires_at: None,
    };
    let mut last = None;
    for fresh in [false, true] {
        // a failure with a cached route may be a stale route: asked once more with a fresh one
        let attempt = (|| -> Result<wechat_protocol::media::DownloadedMedia> {
            let cdn = CdnClient::new(cdn_config(app, &client, fresh)?, Duration::from_secs(60))?;
            let d = descriptor.clone();
            let got = app.rt.block_on(async {
                tokio::time::timeout(
                    Duration::from_secs(110),
                    tokio::task::spawn_blocking(move || cdn.download(&d)),
                )
                .await
            });
            match got {
                Ok(Ok(r)) => r,
                Ok(Err(_)) => Err(anyhow!("download stopped")),
                Err(_) => Err(err("network", "下载超时")),
            }
        })();
        match attempt {
            Ok(media) => {
                if media.bytes.len() as u64 > most {
                    return Err(err("too_large", "文件太大"));
                }
                write_file(&path, &media.bytes)?;
                return Ok(json!({"bytes": media.bytes.len(), "md5": media.md5, "picture_format": media.picture_format}));
            }
            Err(e) => {
                let shown = media_error(&e);
                log(&format!("media download failed: {}", shown));
                let retry = !fresh && matches!(kind_of(&shown), "network");
                last = Some(shown);
                if !retry {
                    break;
                }
            }
        }
    }
    Err(last.unwrap_or_else(|| err("network", "下载没成功")))
}

/// A voice note as a WAV file at "path": from "silk" (a SILK file the backend kept from the message itself), or
/// downloaded by "server_id" and "length" (its SILK size in bytes).
fn op_voice(app: &App, p: &Value) -> Result<Value> {
    let path = target_path(p)?;
    let silk = if let Some(file) = p.get("silk").and_then(Value::as_str).filter(|s| !s.is_empty()) {
        let meta = std::fs::metadata(file).map_err(|_| err("input", "语音文件不在"))?;
        if meta.len() > wechat_protocol::silk::MAX_STREAM_BYTES as u64 {
            return Err(err("too_large", "语音太长"));
        }
        std::fs::read(file).map_err(|_| err("input", "语音文件读不出来"))?
    } else {
        let client = app.client()?;
        let server_id: u64 = str_arg(p, "server_id")?.parse().map_err(|_| err("input", "server_id"))?;
        let length = p.get("length").and_then(Value::as_u64).unwrap_or(0);
        if length == 0 || length > wechat_protocol::silk::MAX_STREAM_BYTES as u64 {
            return Err(err("input", "语音长度不对"));
        }
        let length = length as u32;
        let mut silk = Vec::with_capacity(length as usize);
        let mut offset = 0u32;
        for _ in 0..64 {
            let c = client.clone();
            let chunk = app
                .rt
                .block_on(async {
                    tokio::time::timeout(Duration::from_secs(20), c.download_voice_chunk(server_id, length, offset)).await
                })
                .map_err(|_| err("network", "下载语音超时"))?
                .map_err(|e| media_error(&e))?;
            if chunk.offset != offset || chunk.bytes.is_empty() {
                return Err(err("integrity", "语音分段对不上"));
            }
            silk.extend_from_slice(&chunk.bytes);
            offset = chunk.next_offset;
            if chunk.complete {
                break;
            }
        }
        if offset != length {
            return Err(err("integrity", "语音没有下载完整"));
        }
        silk
    };
    let pcm = wechat_protocol::silk::decode(&silk).map_err(|_| err("integrity", "语音解码不了"))?;
    let wav = wechat_protocol::silk::wav(&pcm).map_err(|_| err("integrity", "语音转换不了"))?;
    write_file(&path, &wav)?;
    let ms = pcm.len() as u64 * 1000 / wechat_protocol::silk::SAMPLE_RATE as u64;
    Ok(json!({"bytes": wav.len(), "ms": ms}))
}

// ---------------------------------------------------------------- main

fn answer(app: &App, id: &Value, result: Result<Value>) {
    match result {
        Ok(mut v) => {
            if let Some(o) = v.as_object_mut() {
                o.insert("id".into(), id.clone());
                o.insert("ok".into(), Value::Bool(true));
            }
            app.out.line(&v);
        }
        Err(e) => {
            let kind = kind_of(&e);
            let message = match kinded(&e) {
                Some(k) => k.message.clone(),
                None => format!("{e:#}"),
            };
            app.out.line(&json!({"id": id, "ok": false, "error": {"kind": kind, "message": message}}));
        }
    }
}

fn handle(app: Arc<App>, req: Value) {
    let id = req.get("id").cloned().unwrap_or(Value::Null);
    let op = req.get("op").and_then(Value::as_str).unwrap_or("").to_owned();
    let result = match op.as_str() {
        "hello" => Ok(json!({"protocol": PROTOCOL, "version": env!("CARGO_PKG_VERSION")})),
        "login_start" => op_login_start(&app, &req),
        "login_poll" => op_login_poll(&app),
        "login_finish" => op_login_finish(&app),
        "restore" => op_restore(&app, &req),
        "listen" => op_listen(app.clone(), &req),
        "stop" => {
            app.stop_listening(req.get("gen").and_then(Value::as_u64).or(Some(0)));
            Ok(json!({}))
        }
        "send" => op_send(&app, &req),
        "contacts" => op_contacts(&app, &req),
        "room" => op_room(&app, &req),
        "groups" => op_groups(&app),
        "media" => op_media(&app, &req),
        "voice" => op_voice(&app, &req),
        "logout" => op_logout(&app),
        _ => Err(err("unsupported", &format!("不认识的操作 {op}"))),
    };
    answer(&app, &id, result);
}

fn main() -> Result<()> {
    let out = Arc::new(Out(Mutex::new(std::io::stdout())));
    let rt = tokio::runtime::Builder::new_multi_thread().worker_threads(2).enable_all().build()?;
    let persist = Arc::new(Persist {
        out: out.clone(),
        seq: AtomicU64::new(0),
        waiting: Mutex::new(HashMap::new()),
        latest: Mutex::new(None),
    });
    let app = Arc::new(App {
        out,
        rt,
        persist,
        login: Mutex::new(None),
        login_seq: AtomicU64::new(0),
        client: Mutex::new(None),
        own: Mutex::new(String::new()),
        gen: AtomicU64::new(0),
        listening: Mutex::new(None),
        intents: Mutex::new(VecDeque::new()),
        cdn: Mutex::new(None),
    });
    let stdin = std::io::stdin();
    for line in stdin.lock().lines() {
        let line = match line {
            Ok(l) => l,
            Err(_) => break,
        };
        if line.trim().is_empty() {
            continue;
        }
        let req: Value = match serde_json::from_str(&line) {
            Ok(v) => v,
            Err(_) => {
                log("a line that is not JSON was ignored");
                continue;
            }
        };
        if req.get("op").and_then(Value::as_str) == Some("persist_ack") {
            let seq = req.get("seq").and_then(Value::as_u64).unwrap_or(0);
            app.persist.ack(seq, req.get("ok").and_then(Value::as_bool).unwrap_or(false));
            continue;
        }
        let app = app.clone();
        std::thread::spawn(move || handle(app, req));
    }
    // stdin closed: the extension is going away
    app.stop_listening(None);
    Ok(())
}
