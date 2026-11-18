//! matewechat-native: OpenMate's link to WeChat, for the openmate-wechat extension.
//!
//! It speaks JSON lines on stdin/stdout with the extension's Python backend, which owns everything else (the store,
//! the inbox, /agent, 自动代答). Only what those two features need is here: QR sign-in, restoring a saved sign-in,
//! the live message stream (push, sync, heartbeat), text sends (with real @s and quotes) and names.
//!
//! The protocol is thisnick/agent-wechat's `wechat-protocol` library (MIT), used through its public API only; the
//! online loop follows that project's daemon: heartbeat at the server's interval (30–180 s), a catch-up drain (scene
//! 6) after each connect, scene 1 on a notification, scene 7 when a heartbeat says so.
//!
//! Lines in:  {"id":N,"op":"…",…}               a request; the answer is {"id":N,"ok":true,…} or {"id":N,"ok":false,"error":{"kind","message"}}
//!            {"op":"persist_ack","seq":S,"ok":bool}   the backend sealed (or failed to seal) a rotated sign-in
//! Lines out: answers, and events without an id: message, cursor, link, persist, auth_lost.
//! Credentials go out only in login_finish's answer and in persist events; never to stderr.

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
    DeviceProfile, LoginAttempt, LoginStatus, NativeClient, NativeOptions, NativeRequestNotSubmitted,
    ServiceRejected, TextSendOptions,
};

const PROTOCOL: u32 = 1;
const DEVICE_TYPE: &str = "UnifiedPCLinux x86_64";
const COMPUTER_NAME: &str = "OpenMate";
const LOGIN_POLL_MOST: Duration = Duration::from_secs(6); // the hub forwards an extension call for 10 s at most
const PERSIST_WAIT: Duration = Duration::from_secs(30);
const QUOTES_KEPT: usize = 5000;
const SENT_KEPT: usize = 500;
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

// ---------------------------------------------------------------- persistence of rotated credentials

/// The protocol library calls this before a renewed sign-in takes effect: the backend must seal it first.
struct Persist {
    out: Arc<Out>,
    seq: AtomicU64,
    waiting: Mutex<HashMap<u64, mpsc::Sender<bool>>>,
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
        if ok {
            Ok(())
        } else {
            bail!("the rotated sign-in was not stored")
        }
    }
}

// ---------------------------------------------------------------- state

struct Login {
    boot: NativeClient,
    attempt: LoginAttempt,
}

struct App {
    out: Arc<Out>,
    rt: tokio::runtime::Runtime,
    persist: Arc<Persist>,
    login: Mutex<Option<Login>>,
    client: Mutex<Option<NativeClient>>,
    own: Mutex<String>,
    listening: Mutex<Option<Arc<AtomicBool>>>,
    quotes: Mutex<(HashMap<String, message::Quotable>, VecDeque<String>)>,
    sent: Mutex<VecDeque<String>>,
}

impl App {
    fn client(&self) -> Result<NativeClient> {
        self.client.lock().map_err(|_| anyhow!("lock"))?.clone().ok_or_else(|| err("signed_out", "微信还没登录"))
    }
    fn own(&self) -> String {
        self.own.lock().map(|o| o.clone()).unwrap_or_default()
    }
    fn remember_quote(&self, q: message::Quotable) {
        if let Ok(mut g) = self.quotes.lock() {
            let (map, order) = &mut *g;
            if map.insert(q.server_id.clone(), q.clone()).is_none() {
                order.push_back(q.server_id);
                while order.len() > QUOTES_KEPT {
                    if let Some(old) = order.pop_front() {
                        map.remove(&old);
                    }
                }
            }
        }
    }
    fn remember_sent(&self, id: &str) {
        if id.is_empty() {
            return;
        }
        if let Ok(mut s) = self.sent.lock() {
            s.push_back(id.to_owned());
            while s.len() > SENT_KEPT {
                s.pop_front();
            }
        }
    }
    fn was_sent_here(&self, id: &str) -> bool {
        self.sent.lock().map(|s| s.iter().any(|x| x == id)).unwrap_or(false)
    }
    fn adopt(&self, state: AccountState) -> Result<String> {
        let own = state.own_username.clone();
        let client = NativeClient::from_account_with_persistence(state, NativeOptions::default(), self.persist.clone())?;
        self.stop_listening();
        if let Some(old) = self.client.lock().map_err(|_| anyhow!("lock"))?.replace(client) {
            let _ = self.rt.block_on(old.shutdown());
        }
        *self.own.lock().map_err(|_| anyhow!("lock"))? = own.clone();
        Ok(own)
    }
    fn stop_listening(&self) {
        if let Some(stop) = self.listening.lock().ok().and_then(|mut l| l.take()) {
            stop.store(true, Ordering::SeqCst);
        }
    }
}

/// An error with a kind the backend maps (signed_out, expired, rejected, not_sent, uncertain, unsupported, input…).
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
fn kind_of(e: &anyhow::Error) -> &'static str {
    if let Some(k) = e.chain().find_map(|c| c.downcast_ref::<Kinded>()) {
        return k.kind;
    }
    if is_session_rejected(e) {
        return "expired";
    }
    "error"
}

// ---------------------------------------------------------------- ops

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
    let boot = NativeClient::new(NativeOptions::default())?;
    let attempt = boot.start_qr_login(device, COMPUTER_NAME)?;
    let url = decode_qr(&attempt.qr_png)?;
    let out = json!({"qr": url, "expires_at": attempt.expires_at, "device": serde_json::to_value(&attempt.device)?});
    *app.login.lock().map_err(|_| anyhow!("lock"))? = Some(Login { boot, attempt });
    Ok(out)
}

fn op_login_poll(app: &App) -> Result<Value> {
    let mut guard = app.login.lock().map_err(|_| anyhow!("lock"))?;
    let login = guard.as_mut().ok_or_else(|| err("input", "没有进行中的扫码登录"))?;
    let deadline = Instant::now() + LOGIN_POLL_MOST;
    let status = match login.boot.poll_login_guarded(&mut login.attempt, deadline, || false) {
        Ok(s) => s,
        Err(e) if is_retryable_login_poll_error(&e) => LoginStatus::Pending,
        Err(e) => {
            if let Some(reason) = wechat_protocol::login_lan_error_reason(&e) {
                return Err(err("lan", &format!("微信要求手机和这台电脑在同一个局域网里确认登录（{reason}）")));
            }
            return Err(e);
        }
    };
    let s = match status {
        LoginStatus::Pending => "pending",
        LoginStatus::Scanned => "scanned",
        LoginStatus::Approved | LoginStatus::Completed => "confirmed",
        LoginStatus::Expired => "expired",
        LoginStatus::Cancelled => "cancelled",
        LoginStatus::Rejected => "rejected",
    };
    Ok(json!({"status": s}))
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
    let mut attempt = login.attempt;
    let state = login.boot.complete_login(&mut attempt)?;
    let saved = serde_json::to_value(&state)?;
    let own = app.adopt(state)?;
    let name = account_name(app, &app.client()?, &own);
    Ok(json!({"state": saved, "account": {"id": own, "name": name}}))
}

fn op_restore(app: &App, p: &Value) -> Result<Value> {
    let state: AccountState =
        serde_json::from_value(p.get("state").cloned().unwrap_or(Value::Null)).map_err(|_| err("expired", "保存的登录信息读不出来"))?;
    let own = app.adopt(state)?;
    let name = account_name(app, &app.client()?, &own);
    Ok(json!({"account": {"id": own, "name": name}}))
}

fn op_logout(app: &App) -> Result<Value> {
    app.stop_listening();
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
    app.stop_listening();
    let stop = Arc::new(AtomicBool::new(false));
    *app.listening.lock().map_err(|_| anyhow!("lock"))? = Some(stop.clone());
    std::thread::Builder::new().name("listen".into()).spawn(move || listen_loop(app, client, cursor, stop))?;
    Ok(json!({}))
}

fn listen_loop(app: Arc<App>, client: NativeClient, mut cursor: Vec<u8>, stop: Arc<AtomicBool>) {
    let mut backoff = Duration::from_secs(1);
    while !stop.load(Ordering::SeqCst) {
        let started = Instant::now();
        let result = session(&app, &client, &mut cursor, &stop);
        app.out.line(&json!({"event": "link", "up": false}));
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
            app.out.line(&json!({"event": "auth_lost", "message": "微信登录已失效，需要重新扫码"}));
            return;
        }
        log(&format!("link down: {e:#}"));
        if started.elapsed() > Duration::from_secs(60) {
            backoff = Duration::from_secs(1);
        }
        let until = Instant::now() + backoff;
        while Instant::now() < until && !stop.load(Ordering::SeqCst) {
            std::thread::sleep(Duration::from_millis(200));
        }
        backoff = (backoff * 2).min(Duration::from_secs(60));
    }
}

fn heartbeat_interval(next: u32) -> Duration {
    Duration::from_secs((next as u64).clamp(30, 180))
}

fn session(app: &App, client: &NativeClient, cursor: &mut Vec<u8>, stop: &AtomicBool) -> Result<()> {
    let mut conn = client.open_push_connection_blocking()?;
    let beat = conn.heartbeat(cursor)?;
    drain(app, &mut conn, cursor, 6, "catchup")?; // what came while away: stored, never acted on
    app.out.line(&json!({"event": "link", "up": true}));
    let mut next_beat = Instant::now() + heartbeat_interval(beat.next_interval);
    loop {
        if stop.load(Ordering::SeqCst) {
            return Ok(());
        }
        if Instant::now() >= next_beat {
            let beat = conn.heartbeat(cursor)?;
            next_beat = Instant::now() + heartbeat_interval(beat.next_interval);
            if beat.selector & 7 != 0 {
                drain(app, &mut conn, cursor, 7, "live")?;
            }
        }
        if let Some(n) = conn.poll(Duration::from_secs(1))? {
            if n.command != 356 {
                // 356 is a contact change; names are read when shown
                drain(app, &mut conn, cursor, 1, "live")?;
            }
        }
    }
}

fn drain(
    app: &App,
    conn: &mut wechat_protocol::AccountPushConnection,
    cursor: &mut Vec<u8>,
    scene: u32,
    origin: &str,
) -> Result<()> {
    let own = app.own();
    for _ in 0..8 {
        let batch = conn.sync(cursor, scene)?;
        for command in &batch.commands {
            if command.id != 5 {
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
            let origin = if m.outgoing && app.was_sent_here(&server_id) { "local_send" } else { origin };
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
            let (event, quotable) = message::normalize(&raw, &own, origin);
            if let Some(q) = quotable {
                app.remember_quote(q);
            }
            app.out.line(&event);
        }
        if !batch.cursor.is_empty() {
            *cursor = batch.cursor.clone();
        }
        // after the batch's messages: the backend stores the cursor once it has handled them
        app.out.line(&json!({"event": "cursor", "cursor": B64.encode(&*cursor)}));
        if !batch.continuation {
            return Ok(());
        }
    }
    bail!("too many sync pages in a row")
}

// ---------------------------------------------------------------- sends

fn quote_reference(q: &message::Quotable, own: &str, display: &str) -> wechat_protocol::quote_send::QuoteReference {
    // agent-wechat's daemon: from_user is me for my own message, the group for a group member's, else the sender;
    // chat_user is the sender
    let from_user = if q.outgoing {
        own.to_owned()
    } else if message::is_group(&q.chat) {
        q.chat.clone()
    } else {
        q.sender.clone()
    };
    wechat_protocol::quote_send::QuoteReference {
        message_type: q.message_type,
        server_id: q.server_id.clone(),
        from_user,
        chat_user: if q.outgoing { own.to_owned() } else { q.sender.clone() },
        display_name: if display.is_empty() { q.sender.clone() } else { display.to_owned() },
        content: q.content.clone(),
        created_at: q.created_at,
        msg_source: q.msgsource.clone(),
    }
}

/// How a send ended: "sent" (the server took it), "rejected" (the server refused it), "not_sent" (nothing left this
/// program) or "uncertain" (it may have gone out: never sent again blindly).
fn send_outcome(e: &anyhow::Error) -> (&'static str, String) {
    if e.chain().any(|c| c.downcast_ref::<NativeRequestNotSubmitted>().is_some()) {
        return ("not_sent", format!("{e:#}"));
    }
    if let Some(r) = ServiceRejected::find(e) {
        return ("rejected", format!("微信拒绝了这条消息（{}）", r.code));
    }
    if let Some(k) = e.chain().find_map(|c| c.downcast_ref::<Kinded>()) {
        return (if k.kind == "rejected" { "rejected" } else { "not_sent" }, k.message.clone());
    }
    ("uncertain", format!("{e:#}"))
}

fn op_send(app: &App, p: &Value) -> Result<Value> {
    let chat = str_arg(p, "chat")?.to_owned();
    let text = str_arg(p, "text")?.to_owned();
    let key = str_arg(p, "key")?.to_owned();
    let reply_to = p.get("reply_to").and_then(Value::as_str).unwrap_or("").to_owned();
    let mut members: Vec<(String, String)> = vec![];
    for m in p.get("mentions").and_then(Value::as_array).cloned().unwrap_or_default() {
        let id = m.get("id").and_then(Value::as_str).unwrap_or("").to_owned();
        let name = m.get("name").and_then(Value::as_str).unwrap_or("").to_owned();
        if !id.is_empty() {
            members.push((id, name));
        }
    }
    let quote_name = p.get("reply_name").and_then(Value::as_str).unwrap_or("").to_owned();
    let client = app.client()?;
    let own = app.own();
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
        let quoted = if reply_to.is_empty() {
            None
        } else {
            app.quotes.lock().ok().and_then(|g| g.0.get(&reply_to).cloned()).filter(|q| q.chat == chat)
        };
        if let Some(q) = quoted {
            let reference = quote_reference(&q, &own, &quote_name);
            let xml = wechat_protocol::quote_send::quote_xml(&own, &rendered, &reference)?;
            let r = app.rt.block_on(client.send_quote(&chat, &client_id.to_string(), created_at, &xml, source.as_deref()))?;
            return match r.status {
                wechat_protocol::media_send::SendStatus::ServerAccepted => Ok(("sent".into(), r.server_message_id)),
                wechat_protocol::media_send::SendStatus::Rejected => {
                    Err(err("rejected", &format!("微信拒绝了这条消息（{}）", r.service_code)))
                }
                wechat_protocol::media_send::SendStatus::NeedsReview => Ok(("uncertain".into(), r.server_message_id)),
            };
        }
        let r = app.rt.block_on(client.send_text(
            &chat,
            &rendered,
            TextSendOptions { created_at, client_message_id: client_id, source },
        ))?;
        if !r.accepted {
            let code = r.item_code.unwrap_or(r.outer_code);
            return Err(err("rejected", &format!("微信拒绝了这条消息（{code}）")));
        }
        let matches = r.returned_destination_matches && r.returned_client_id_matches;
        Ok((if matches { "sent".into() } else { "uncertain".into() }, r.server_message_id))
    })();
    match attempt {
        Ok((status, id)) => {
            let id = id.unwrap_or_default();
            app.remember_sent(&id);
            Ok(json!({"status": status, "message_id": id, "quoted": !reply_to.is_empty()}))
        }
        Err(e) => {
            let (status, why) = send_outcome(&e);
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
        .map(|a| a.iter().filter_map(Value::as_str).map(str::to_owned).take(40).collect())
        .unwrap_or_default();
    let mut found = vec![];
    for pair in ids.chunks(2) {
        // the server takes one or two identities per request
        let r = app.rt.block_on(async { tokio::time::timeout(Duration::from_secs(8), client.lookup_contacts(pair)).await });
        match r {
            Ok(Ok(lookup)) => {
                for c in lookup.contacts {
                    found.push(json!({"id": c.username, "nickname": c.nickname, "remark": c.remark, "alias": c.alias}));
                }
            }
            Ok(Err(e)) => log(&format!("contact lookup failed: {e:#}")),
            Err(_) => log("contact lookup timed out"),
        }
    }
    Ok(json!({"contacts": found}))
}

fn op_room(app: &App, p: &Value) -> Result<Value> {
    let client = app.client()?;
    let id = str_arg(p, "id")?.to_owned();
    if !message::is_group(&id) {
        return Err(err("input", "不是群"));
    }
    let r = app.rt.block_on(async { tokio::time::timeout(Duration::from_secs(8), client.lookup_room(&id, 0)).await });
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
    Ok(json!({"id": roster.room_id, "members": members, "owner": roster.owner}))
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
            let message = match e.chain().find_map(|c| c.downcast_ref::<Kinded>()) {
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
            app.stop_listening();
            Ok(json!({}))
        }
        "send" => op_send(&app, &req),
        "contacts" => op_contacts(&app, &req),
        "room" => op_room(&app, &req),
        "logout" => op_logout(&app),
        _ => Err(err("unsupported", &format!("不认识的操作 {op}"))),
    };
    answer(&app, &id, result);
}

fn main() -> Result<()> {
    let out = Arc::new(Out(Mutex::new(std::io::stdout())));
    let rt = tokio::runtime::Builder::new_multi_thread().worker_threads(2).enable_all().build()?;
    let persist = Arc::new(Persist { out: out.clone(), seq: AtomicU64::new(0), waiting: Mutex::new(HashMap::new()) });
    let app = Arc::new(App {
        out,
        rt,
        persist,
        login: Mutex::new(None),
        client: Mutex::new(None),
        own: Mutex::new(String::new()),
        listening: Mutex::new(None),
        quotes: Mutex::new((HashMap::new(), VecDeque::new())),
        sent: Mutex::new(VecDeque::new()),
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
    app.stop_listening();
    Ok(())
}
