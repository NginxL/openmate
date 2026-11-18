//! One synced message, normalized for OpenMate: who said it in which chat, what it says, whom it @-mentions and what
//! it quotes. Pure functions, no network: the unit tests below cover them.
//!
//! The @-list rule follows agent-wechat's daemon (MIT): only the message's own top-level `<msgsource><atuserlist>`
//! counts (a quoted message's list does not), ids are comma-separated, `notify@all` is @everyone, and anything odd
//! makes the list unknown, which never counts as @me.

use serde_json::{json, Value};

pub const MENTION_SPACE: char = '\u{2005}';

/// What the protocol library decoded, reduced to the fields used here.
pub struct Raw<'a> {
    pub chat: &'a str,
    pub from: &'a str,
    pub outgoing: bool,
    pub server_id: &'a str,
    pub message_type: i32,
    pub content: &'a str,
    pub created_at: u32,
    pub msgsource: Option<&'a str>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Mentions {
    pub known: bool,
    pub ids: Vec<String>,
    pub all: bool,
}

/// What a later quote of this message needs (agent-wechat's QuoteReference fields, see quote_reference in main.rs).
#[allow(dead_code)]
#[derive(Debug, Clone)]
pub struct Quotable {
    pub message_type: i32,
    pub server_id: String,
    pub chat: String,
    pub sender: String,
    pub outgoing: bool,
    pub content: String,
    pub created_at: u32,
    pub msgsource: Option<String>,
}

/// A group: an ordinary one (…@chatroom), a WeCom interop one (…@im.chatroom) or a WeCom group (…@qy_g),
/// as agent-wechat's daemon classifies them.
pub fn is_group(chat: &str) -> bool {
    chat.contains("@chatroom") || chat.ends_with("@im.chatroom") || chat.ends_with("@qy_g")
}

/// The @-list of a message's own msgsource.
pub fn mentions(source: Option<&str>) -> Mentions {
    let unknown = Mentions { known: false, ids: vec![], all: false };
    let Some(source) = source.filter(|s| !s.trim().is_empty()) else {
        return Mentions { known: true, ids: vec![], all: false };
    };
    if source.len() > 64 * 1024 {
        return unknown;
    }
    let Ok(doc) = roxmltree::Document::parse(source) else {
        return unknown;
    };
    let root = doc.root_element();
    if root.tag_name().name() != "msgsource" || doc.descendants().count() > 4096 {
        return unknown;
    }
    let lists: Vec<_> = root
        .children()
        .filter(|n| n.is_element() && n.tag_name().name() == "atuserlist")
        .collect();
    if lists.len() > 1 || lists.iter().any(|n| n.children().any(|c| c.is_element())) {
        return unknown;
    }
    let raw = lists.first().and_then(|n| n.text()).unwrap_or("");
    let ids: Vec<&str> = raw.split(',').map(str::trim).filter(|s| !s.is_empty()).collect();
    if ids.len() > 128 || ids.iter().any(|s| s.len() > 256 || s.contains(['\0', '\n', '\r'])) {
        return unknown;
    }
    Mentions {
        known: true,
        all: ids.contains(&"notify@all"),
        ids: ids.into_iter().filter(|id| *id != "notify@all").map(str::to_owned).collect(),
    }
}

/// A group message from someone else starts with "<sender id>:\n"; returns (sender, rest).
pub fn split_sender(content: &str) -> Option<(&str, &str)> {
    let (head, rest) = content.split_once(":\n")?;
    let ok = !head.is_empty()
        && head.len() <= 128
        && head.bytes().all(|b| b.is_ascii_alphanumeric() || b"_-@.".contains(&b));
    ok.then_some((head, rest))
}

fn xml_text<'a>(node: roxmltree::Node<'a, 'a>, path: &[&str]) -> Option<String> {
    let mut cur = node;
    for name in path {
        cur = cur.children().find(|c| c.is_element() && c.tag_name().name() == *name)?;
    }
    let text: String = cur.descendants().filter(|n| n.is_text()).filter_map(|n| n.text()).collect();
    // ordinary whitespace only: a real @'s U+2005 at the end of a quote's text marks it for the @ parsing
    Some(text.trim_matches(|c: char| c.is_ascii_whitespace() || c == '\u{3000}').to_owned())
}

/// An app message (type 49): (OpenMate type, text, quoted server id, quoted sender, quoted text).
fn app_message(content: &str) -> (&'static str, String, String, String, String) {
    let Ok(doc) = roxmltree::Document::parse(content.trim()) else {
        return ("CARD", "[卡片消息]".into(), String::new(), String::new(), String::new());
    };
    let root = doc.root_element();
    let appmsg = if root.tag_name().name() == "appmsg" {
        Some(root)
    } else {
        root.children().find(|c| c.is_element() && c.tag_name().name() == "appmsg")
    };
    let Some(app) = appmsg else {
        return ("CARD", "[卡片消息]".into(), String::new(), String::new(), String::new());
    };
    let title = xml_text(app, &["title"]).unwrap_or_default();
    let kind = xml_text(app, &["type"]).unwrap_or_default();
    match kind.as_str() {
        "57" => {
            let id = xml_text(app, &["refermsg", "svrid"]).unwrap_or_default();
            let who = xml_text(app, &["refermsg", "displayname"]).unwrap_or_default();
            let said = xml_text(app, &["refermsg", "content"]).unwrap_or_default();
            ("TEXT", title, id, who, said)
        }
        "6" => ("FILE", format!("[文件] {title}"), String::new(), String::new(), String::new()),
        "19" => ("CARD", format!("[聊天记录] {title}"), String::new(), String::new(), String::new()),
        "33" | "36" => ("CARD", format!("[小程序] {title}"), String::new(), String::new(), String::new()),
        _ if title.is_empty() => ("CARD", "[卡片消息]".into(), String::new(), String::new(), String::new()),
        _ => ("CARD", format!("[链接] {title}"), String::new(), String::new(), String::new()),
    }
}

/// A read the account made on another device (the phone): WeChat syncs it as the account's own type-51 control
/// `<msg><op id="1|2|5"><username>chat</username><name>lastMessage</name><arg>…</arg></op></msg>` (1 read up to a message, 2 opened, 5 left the chat; the
/// shapes agent-wechat's daemon reads). The chat and when; None for anything else.
pub fn read_control(raw: &Raw, own: &str) -> Option<(String, u32)> {
    if raw.message_type != 51 || !raw.outgoing || own.is_empty() || raw.from != own || raw.created_at == 0 {
        return None;
    }
    let body = raw.content.trim();
    if body.len() > 64 * 1024 {
        return None;
    }
    let doc = roxmltree::Document::parse(body).ok()?;
    let root = doc.root_element();
    if root.tag_name().name() != "msg" {
        return None;
    }
    let ops: Vec<_> = root.children().filter(|n| n.is_element()).collect();
    if ops.len() != 1 || ops[0].tag_name().name() != "op" {
        return None; // exactly one operation, as the daemon requires
    }
    let op = ops[0];
    if !matches!(op.attribute("id"), Some("1") | Some("2") | Some("5")) {
        return None;
    }
    if xml_text(op, &["name"]).as_deref() != Some("lastMessage") || xml_text(op, &["arg"]).is_none() {
        return None; // the read controls name the last message read, as the daemon requires
    }
    let chat = xml_text(op, &["username"])?;
    // the control is the account's own message to that very chat: one naming another chat is not taken
    let ok = chat == raw.chat && !chat.is_empty() && chat.len() <= 512 && chat != own && chat != "fmessage"
        && !chat.chars().any(|c| c.is_control() || c.is_whitespace());
    ok.then_some((chat, raw.created_at))
}

/// A CDN AES key as WeChat writes it in a media message (32 hex digits), checked.
fn aes_key(v: Option<&str>) -> Option<String> {
    let v = v?.trim();
    (v.len() == 32 && v.bytes().all(|b| b.is_ascii_hexdigit())).then(|| v.to_ascii_lowercase())
}

/// A CDN file id (or URL) safe to hand back: not empty, bounded, no control characters.
fn file_id(v: Option<&str>) -> Option<String> {
    let v = v?.trim();
    (!v.is_empty() && v.len() <= 8192 && !v.chars().any(char::is_control)).then(|| v.to_owned())
}

fn number(v: Option<&str>) -> Option<u64> {
    v?.trim().parse::<u64>().ok()
}

fn put(o: &mut serde_json::Map<String, Value>, k: &str, v: Option<impl Into<Value>>) {
    if let Some(v) = v {
        o.insert(k.to_owned(), v.into());
    }
}

/// The first element called `name` under the document's root (or the root itself).
fn element<'a>(doc: &'a roxmltree::Document<'a>, name: &str) -> Option<roxmltree::Node<'a, 'a>> {
    doc.descendants().find(|n| n.is_element() && n.tag_name().name() == name)
}

/// What a later download of a media message needs (the CDN file ids and keys, sizes), and for a forwarded chat record
/// its lines; None for anything else or anything unreadable. It stays in the extension's own database: the keys
/// open only these files, and never go to the hub or a log.
pub fn media_info(message_type: i32, body: &str) -> Option<Value> {
    let body = body.trim();
    if body.is_empty() || body.len() > 256 * 1024 {
        return None;
    }
    let doc = roxmltree::Document::parse(body).ok()?;
    if doc.descendants().count() > 20_000 {
        return None;
    }
    let mut o = serde_json::Map::new();
    match message_type {
        3 => {
            let img = element(&doc, "img")?;
            let a = |k: &str| img.attribute(k);
            o.insert("kind".into(), "image".into());
            put(&mut o, "aes_key", aes_key(a("aeskey")));
            put(&mut o, "mid", file_id(a("cdnmidimgurl")));
            put(&mut o, "big", file_id(a("cdnbigimgurl")));
            put(&mut o, "thumb", file_id(a("cdnthumburl")));
            put(&mut o, "thumb_aes_key", aes_key(a("cdnthumbaeskey")));
            put(&mut o, "size", number(a("length")));
            put(&mut o, "hd_size", number(a("hdlength")));
            put(&mut o, "width", number(a("cdnmidwidth")).or(number(a("cdnhdwidth"))));
            put(&mut o, "height", number(a("cdnmidheight")).or(number(a("cdnhdheight"))));
        }
        34 => {
            let v = element(&doc, "voicemsg")?;
            o.insert("kind".into(), "voice".into());
            put(&mut o, "length", number(v.attribute("length")));
            put(&mut o, "ms", number(v.attribute("voicelength")));
        }
        43 | 62 => {
            let v = element(&doc, "videomsg")?;
            let a = |k: &str| v.attribute(k);
            o.insert("kind".into(), "video".into());
            put(&mut o, "aes_key", aes_key(a("aeskey")));
            put(&mut o, "url", file_id(a("cdnvideourl")));
            put(&mut o, "thumb", file_id(a("cdnthumburl")));
            put(&mut o, "thumb_aes_key", aes_key(a("cdnthumbaeskey")));
            put(&mut o, "size", number(a("length")));
            put(&mut o, "seconds", number(a("playlength")));
            put(&mut o, "md5", a("md5").filter(|m| m.len() == 32 && m.bytes().all(|b| b.is_ascii_hexdigit())).map(str::to_ascii_lowercase));
            put(&mut o, "width", number(a("cdnthumbwidth")));
            put(&mut o, "height", number(a("cdnthumbheight")));
        }
        49 => {
            let app = element(&doc, "appmsg")?;
            match xml_text(app, &["type"]).as_deref() {
                Some("6") => {
                    o.insert("kind".into(), "file".into());
                    put(&mut o, "name", xml_text(app, &["title"]).filter(|t| !t.is_empty()));
                    put(&mut o, "ext", xml_text(app, &["appattach", "fileext"]).filter(|t| !t.is_empty()));
                    put(&mut o, "size", number(xml_text(app, &["appattach", "totallen"]).as_deref()));
                    put(&mut o, "url", file_id(xml_text(app, &["appattach", "cdnattachurl"]).as_deref()));
                    put(&mut o, "aes_key", aes_key(xml_text(app, &["appattach", "aeskey"]).as_deref()));
                }
                Some("19") => {
                    o.insert("kind".into(), "record".into());
                    put(&mut o, "title", xml_text(app, &["title"]).filter(|t| !t.is_empty()));
                    put(&mut o, "desc", xml_text(app, &["des"]).filter(|t| !t.is_empty()));
                    let items = xml_text(app, &["recorditem"]).map(|r| record_items(&r)).unwrap_or_default();
                    o.insert("items".into(), Value::Array(items));
                }
                _ => return None,
            }
        }
        _ => return None,
    }
    Some(Value::Object(o))
}

/// What a message carries inline that is worth keeping: a voice note's SILK stream (WeChat sends short notes whole),
/// or a picture's thumbnail (JPEG or PNG). None for anything else, or anything too big or not of that shape.
pub fn inline_media(message_type: i32, data: &[u8]) -> Option<&[u8]> {
    match message_type {
        34 if data.len() >= 12 && data.len() <= 512 * 1024 && data[0] <= 3 && &data[1..10] == b"#!SILK_V3" => Some(data),
        3 if data.len() <= 256 * 1024 && (data.starts_with(&[0xff, 0xd8, 0xff]) || data.starts_with(b"\x89PNG")) => Some(data),
        _ => None,
    }
}

/// A forwarded chat record's lines (`<recordinfo><datalist><dataitem datatype=…>`), at most 200, nested records
/// as their title.
pub fn record_items(record: &str) -> Vec<Value> {
    let record = record.trim();
    if record.is_empty() || record.len() > 256 * 1024 {
        return vec![];
    }
    let Ok(doc) = roxmltree::Document::parse(record) else {
        return vec![];
    };
    let Some(list) = element(&doc, "datalist") else {
        return vec![];
    };
    let mut out = vec![];
    for item in list.children().filter(|n| n.is_element() && n.tag_name().name() == "dataitem").take(200) {
        let get = |k: &str| xml_text(item, &[k]).filter(|t| !t.is_empty());
        let kind = item.attribute("datatype").unwrap_or("").to_owned();
        let mut o = serde_json::Map::new();
        o.insert("type".into(), kind.into());
        put(&mut o, "who", get("sourcename"));
        put(&mut o, "when", get("sourcetime"));
        put(&mut o, "text", get("datadesc"));
        put(&mut o, "title", get("datatitle"));
        put(&mut o, "ext", get("datafmt"));
        out.push(Value::Object(o));
    }
    out
}

/// The message event for OpenMate, and what a quote of it needs (text messages only).
pub fn normalize(raw: &Raw, own: &str, origin: &str) -> (Value, Option<Quotable>) {
    let group = is_group(raw.chat);
    let (sender, body) = if raw.outgoing {
        (own.to_owned(), raw.content.to_owned())
    } else if group {
        match split_sender(raw.content) {
            Some((s, rest)) => (s.to_owned(), rest.to_owned()),
            None => (String::new(), raw.content.to_owned()),
        }
    } else {
        (raw.from.to_owned(), raw.content.to_owned())
    };
    let m = mentions(raw.msgsource);
    let at_me = !raw.outgoing && m.known && !own.is_empty() && m.ids.iter().any(|id| id == own);
    let at_all = !raw.outgoing && m.known && m.all;
    let (kind, text, parent, quoted_who, quoted_text) = match raw.message_type {
        1 => ("TEXT", body.clone(), String::new(), String::new(), String::new()),
        3 => ("IMAGE", "[图片]".into(), String::new(), String::new(), String::new()),
        34 => ("AUDIO", "[语音]".into(), String::new(), String::new(), String::new()),
        42 => ("CARD", "[名片]".into(), String::new(), String::new(), String::new()),
        43 => ("VIDEO", "[视频]".into(), String::new(), String::new(), String::new()),
        47 => ("STICKER", "[表情]".into(), String::new(), String::new(), String::new()),
        48 => ("CARD", "[位置]".into(), String::new(), String::new(), String::new()),
        49 => app_message(&body),
        50 => ("SYSTEM", "[通话]".into(), String::new(), String::new(), String::new()),
        10000 => ("SYSTEM", body.clone(), String::new(), String::new(), String::new()),
        10002 => ("SYSTEM", "[撤回了一条消息]".into(), String::new(), String::new(), String::new()),
        _ => ("SYSTEM", "[暂不支持的消息]".into(), String::new(), String::new(), String::new()),
    };
    let mut event = json!({
        "event": "message",
        "origin": origin,
        "id": raw.server_id,
        "chat": raw.chat,
        "group": group,
        "sender": sender,
        "mine": raw.outgoing,
        "type": kind,
        "wire_type": raw.message_type,
        "text": text,
        "at": raw.created_at,
        "at_me": at_me,
        "at_all": at_all,
        "mentions_known": m.known,
        "parent": parent,
        "quoted_sender": quoted_who,
        "quoted_text": quoted_text,
        // for a later quote of this message (text only): its body without the group envelope, and its own source
        "quotable": raw.message_type == 1 && !raw.server_id.is_empty() && (raw.outgoing || !sender.is_empty()),
        "content": if raw.message_type == 1 { body.as_str() } else { "" },
        "msgsource": if raw.message_type == 1 { raw.msgsource.unwrap_or("") } else { "" },
    });
    if let Some(media) = media_info(raw.message_type, &body) {
        event["media"] = media;
    }
    let quotable = (raw.message_type == 1 && !raw.server_id.is_empty() && (raw.outgoing || !sender.is_empty())).then(|| {
        Quotable {
            message_type: 1,
            server_id: raw.server_id.to_owned(),
            chat: raw.chat.to_owned(),
            sender: sender.clone(),
            outgoing: raw.outgoing,
            content: body,
            created_at: raw.created_at,
            msgsource: raw.msgsource.map(str::to_owned).filter(|s| !s.is_empty()),
        }
    });
    (event, quotable)
}

/// A name safe to put after "@" on the wire (agent-wechat's rule): not blank, no control characters, no U+2005.
pub fn safe_mention_name(name: &str) -> bool {
    !name.trim().is_empty() && name.len() <= 1024 && !name.chars().any(|c| c.is_control() || c == MENTION_SPACE)
}

/// A member id that may go into an atuserlist.
pub fn safe_member_id(id: &str) -> bool {
    !id.is_empty()
        && id.len() <= 256
        && id != "notify@all"
        && id.bytes().all(|b| b.is_ascii_alphanumeric() || b"_-@.".contains(&b))
}

/// "@name␣" before the text for each mentioned member, and the msgsource that makes them real @s.
pub fn render_mentions(text: &str, members: &[(String, String)]) -> anyhow::Result<(String, Option<String>)> {
    if members.is_empty() {
        return Ok((text.to_owned(), None));
    }
    anyhow::ensure!(members.len() <= 20, "too many mentions");
    let mut rendered = String::new();
    let mut ids = Vec::new();
    for (id, name) in members {
        anyhow::ensure!(safe_member_id(id), "invalid mention id");
        let shown = if safe_mention_name(name) { name.as_str() } else { id.as_str() };
        rendered.push('@');
        rendered.push_str(shown);
        rendered.push(MENTION_SPACE);
        if !ids.contains(id) {
            ids.push(id.clone());
        }
    }
    rendered.push_str(text);
    let source = format!("<msgsource><atuserlist>{}</atuserlist><alnode><fr>1</fr></alnode></msgsource>", ids.join(","));
    Ok((rendered, Some(source)))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn raw<'a>(chat: &'a str, from: &'a str, out: bool, ty: i32, content: &'a str, src: Option<&'a str>) -> Raw<'a> {
        Raw { chat, from, outgoing: out, server_id: "123", message_type: ty, content, created_at: 1_700_000_000, msgsource: src }
    }

    #[test]
    fn mention_list() {
        assert_eq!(mentions(None), Mentions { known: true, ids: vec![], all: false });
        let m = mentions(Some("<msgsource><atuserlist><![CDATA[me,other]]></atuserlist></msgsource>"));
        assert!(m.known && m.ids == vec!["me", "other"] && !m.all);
        let m = mentions(Some("<msgsource><atuserlist>notify@all,x</atuserlist></msgsource>"));
        assert!(m.all && m.ids == vec!["x"]);
        // a quoted message's list does not count
        let m = mentions(Some("<msgsource><quote><atuserlist>me</atuserlist></quote></msgsource>"));
        assert!(m.known && m.ids.is_empty());
        // two lists, or markup inside the list: unknown
        assert!(!mentions(Some("<msgsource><atuserlist>me</atuserlist><atuserlist>x</atuserlist></msgsource>")).known);
        assert!(!mentions(Some("<msgsource><atuserlist>me<x/></atuserlist></msgsource>")).known);
        assert!(!mentions(Some("not xml")).known);
    }

    #[test]
    fn group_message_from_someone_else() {
        let src = "<msgsource><atuserlist>wxid_me</atuserlist></msgsource>";
        let (e, q) = normalize(&raw("1@chatroom", "1@chatroom", false, 1, "wxid_bob:\n@我 在吗", Some(src)), "wxid_me", "live");
        assert_eq!(e["sender"], "wxid_bob");
        assert_eq!(e["text"], "@我 在吗");
        assert_eq!(e["at_me"], true);
        assert_eq!(e["at_all"], false);
        assert_eq!(e["mine"], false);
        let q = q.unwrap();
        assert_eq!(q.sender, "wxid_bob");
        assert_eq!(q.content, "@我 在吗");
    }

    #[test]
    fn typed_name_is_not_a_mention() {
        let (e, _) = normalize(&raw("1@chatroom", "1@chatroom", false, 1, "wxid_bob:\n@法正 你好", None), "wxid_me", "live");
        assert_eq!(e["at_me"], false);
    }

    #[test]
    fn at_all_alone_is_not_at_me() {
        let src = "<msgsource><atuserlist>notify@all</atuserlist></msgsource>";
        let (e, _) = normalize(&raw("1@chatroom", "1@chatroom", false, 1, "wxid_bob:\n通知", Some(src)), "wxid_me", "live");
        assert_eq!(e["at_me"], false);
        assert_eq!(e["at_all"], true);
    }

    #[test]
    fn my_own_message_from_the_phone() {
        let (e, q) = normalize(&raw("1@chatroom", "wxid_me", true, 1, "/agent 总结一下", None), "wxid_me", "live");
        assert_eq!(e["mine"], true);
        assert_eq!(e["sender"], "wxid_me");
        assert_eq!(e["text"], "/agent 总结一下");
        assert_eq!(e["at_me"], false);
        assert!(q.unwrap().outgoing);
        let (e, _) = normalize(&raw("wxid_bob", "wxid_me", true, 1, "/agent hi", None), "wxid_me", "live");
        assert_eq!(e["chat"], "wxid_bob");
        assert_eq!(e["mine"], true);
    }

    #[test]
    fn direct_message() {
        let (e, _) = normalize(&raw("wxid_bob", "wxid_bob", false, 1, "hello", None), "wxid_me", "live");
        assert_eq!(e["sender"], "wxid_bob");
        assert_eq!(e["group"], false);
    }

    #[test]
    fn quote_reply_keeps_a_real_mention_end() {
        let xml = "<msg><appmsg><title>@我\u{2005}</title><type>57</type><refermsg><svrid>1</svrid></refermsg></appmsg></msg>";
        let (e, _) = normalize(&raw("1@chatroom", "1@chatroom", false, 49, &format!("wxid_bob:\n{xml}"), None), "wxid_me", "live");
        assert_eq!(e["text"], "@我\u{2005}");
    }

    #[test]
    fn quote_reply() {
        let xml = "<msg><appmsg appid=\"\" sdkver=\"0\"><title>/agent 看看这个</title><type>57</type><refermsg><type>1</type><svrid>987</svrid><fromusr>1@chatroom</fromusr><chatusr>wxid_bob</chatusr><displayname>Bob</displayname><content>原话</content></refermsg></appmsg><fromusername>wxid_me</fromusername></msg>";
        let (e, q) = normalize(&raw("1@chatroom", "wxid_me", true, 49, xml, None), "wxid_me", "live");
        assert_eq!(e["type"], "TEXT");
        assert_eq!(e["text"], "/agent 看看这个");
        assert_eq!(e["parent"], "987");
        assert_eq!(e["quoted_sender"], "Bob");
        assert!(q.is_none());
    }

    #[test]
    fn placeholders() {
        let (e, _) = normalize(&raw("wxid_bob", "wxid_bob", false, 3, "<msg/>", None), "wxid_me", "live");
        assert_eq!(e["type"], "IMAGE");
        assert_eq!(e["text"], "[图片]");
    }

    #[test]
    fn image_media() {
        let xml = "<?xml version=\"1.0\"?>\n<msg><img aeskey=\"0123456789ABCDEF0123456789abcdef\" cdnthumburl=\"3057thumb\" cdnthumbaeskey=\"fedcba9876543210fedcba9876543210\" cdnmidimgurl=\"3057mid\" length=\"123456\" hdlength=\"0\" cdnmidwidth=\"800\" cdnmidheight=\"600\" md5=\"x\" /></msg>";
        let (e, _) = normalize(&raw("1@chatroom", "1@chatroom", false, 3, &format!("wxid_bob:\n{xml}"), None), "wxid_me", "live");
        assert_eq!(e["text"], "[图片]");
        let m = &e["media"];
        assert_eq!(m["kind"], "image");
        assert_eq!(m["aes_key"], "0123456789abcdef0123456789abcdef");
        assert_eq!(m["mid"], "3057mid");
        assert_eq!(m["thumb"], "3057thumb");
        assert_eq!(m["thumb_aes_key"], "fedcba9876543210fedcba9876543210");
        assert_eq!(m["size"], 123456);
        assert_eq!(m["width"], 800);
        assert!(m.get("big").is_none());
        // a bad key is left out, never passed on half-read
        let (e, _) = normalize(&raw("wxid_bob", "wxid_bob", false, 3, "<msg><img aeskey=\"xyz\" cdnmidimgurl=\"a\"/></msg>", None), "wxid_me", "live");
        assert!(e["media"].get("aes_key").is_none());
        // not XML: no media at all
        let (e, _) = normalize(&raw("wxid_bob", "wxid_bob", false, 3, "<msg/>", None), "wxid_me", "live");
        assert!(e.get("media").is_none());
    }

    #[test]
    fn voice_video_file_media() {
        let v = media_info(34, "<msg><voicemsg endflag=\"1\" voicelength=\"2340\" length=\"3842\" bufid=\"0\"/></msg>").unwrap();
        assert_eq!(v["kind"], "voice");
        assert_eq!(v["length"], 3842);
        assert_eq!(v["ms"], 2340);
        let v = media_info(43, "<msg><videomsg aeskey=\"0123456789abcdef0123456789abcdef\" cdnvideourl=\"vid\" cdnthumburl=\"th\" length=\"900\" playlength=\"12\" md5=\"0123456789abcdef0123456789ABCDEF\"/></msg>").unwrap();
        assert_eq!(v["kind"], "video");
        assert_eq!(v["url"], "vid");
        assert_eq!(v["seconds"], 12);
        assert_eq!(v["md5"], "0123456789abcdef0123456789abcdef");
        let f = media_info(49, "<msg><appmsg><title>报告.pdf</title><type>6</type><appattach><totallen>2048</totallen><fileext>pdf</fileext><cdnattachurl>fid</cdnattachurl><aeskey>0123456789abcdef0123456789abcdef</aeskey></appattach></appmsg></msg>").unwrap();
        assert_eq!(f["kind"], "file");
        assert_eq!(f["name"], "报告.pdf");
        assert_eq!(f["size"], 2048);
        assert_eq!(f["url"], "fid");
        // a link card is no media
        assert!(media_info(49, "<msg><appmsg><title>t</title><type>5</type></appmsg></msg>").is_none());
        assert!(media_info(1, "hello").is_none());
    }

    #[test]
    fn chat_record() {
        let record = "<recordinfo><title>群聊的聊天记录</title><datalist count=\"3\"><dataitem datatype=\"1\" dataid=\"a\"><sourcename>Bob</sourcename><sourcetime>2026-11-17 20:01</sourcetime><datadesc>今晚打本</datadesc></dataitem><dataitem datatype=\"2\" dataid=\"b\"><sourcename>Ann</sourcename><sourcetime>2026-11-17 20:02</sourcetime></dataitem><dataitem datatype=\"8\"><sourcename>Ann</sourcename><datatitle>攻略.pdf</datatitle></dataitem></datalist></recordinfo>";
        let esc = record.replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;");
        let xml = format!("<msg><appmsg><title>群聊的聊天记录</title><des>Bob: 今晚打本</des><type>19</type><recorditem>{esc}</recorditem></appmsg></msg>");
        let (e, _) = normalize(&raw("wxid_bob", "wxid_bob", false, 49, &xml, None), "wxid_me", "live");
        assert_eq!(e["text"], "[聊天记录] 群聊的聊天记录");
        let m = &e["media"];
        assert_eq!(m["kind"], "record");
        let items = m["items"].as_array().unwrap();
        assert_eq!(items.len(), 3);
        assert_eq!(items[0]["who"], "Bob");
        assert_eq!(items[0]["text"], "今晚打本");
        assert_eq!(items[1]["type"], "2");
        assert_eq!(items[2]["title"], "攻略.pdf");
        // CDATA works the same
        let cdata = format!("<msg><appmsg><title>t</title><type>19</type><recorditem><![CDATA[{record}]]></recorditem></appmsg></msg>");
        assert_eq!(media_info(49, &cdata).unwrap()["items"].as_array().unwrap().len(), 3);
    }

    #[test]
    fn inline_data() {
        let mut silk = vec![2u8];
        silk.extend_from_slice(b"#!SILK_V3");
        silk.extend_from_slice(&[1, 0, 9]);
        assert!(inline_media(34, &silk).is_some());
        assert!(inline_media(34, b"not silk at all").is_none());
        assert!(inline_media(3, &[0xff, 0xd8, 0xff, 0xe0]).is_some());
        assert!(inline_media(3, b"wxgf....").is_none());
        assert!(inline_media(1, &[0xff, 0xd8, 0xff]).is_none());
    }

    #[test]
    fn group_ids() {
        assert!(is_group("1@chatroom") && is_group("2@im.chatroom") && is_group("3@qy_g"));
        assert!(!is_group("wxid_bob") && !is_group("x@openim"));
    }

    #[test]
    fn a_read_on_the_phone() {
        let xml = "<msg><op id='2'><username>1@chatroom</username><name>lastMessage</name><arg>x</arg></op></msg>";
        let r = Raw { chat: "1@chatroom", from: "wxid_me", outgoing: true, server_id: "9", message_type: 51, content: xml, created_at: 1_700_000_100, msgsource: None };
        assert_eq!(read_control(&r, "wxid_me"), Some(("1@chatroom".to_string(), 1_700_000_100)));
        let elsewhere = Raw { chat: "2@chatroom", ..r };
        assert_eq!(read_control(&elsewhere, "wxid_me"), None, "the outer chat must be the one it names");
        let other = Raw { from: "wxid_bob", outgoing: false, ..r };
        assert_eq!(read_control(&other, "wxid_me"), None);
        let unnamed = Raw { content: "<msg><op id='2'><username>1@chatroom</username></op></msg>", ..r };
        assert_eq!(read_control(&unnamed, "wxid_me"), None, "only the lastMessage controls");
        let op9 = Raw { content: "<msg><op id='9'><username>x</username></op></msg>", ..r };
        assert_eq!(read_control(&op9, "wxid_me"), None);
    }

    #[test]
    fn rendered_mentions() {
        let (t, s) = render_mentions("你好", &[("wxid_bob".into(), "Bob".into())]).unwrap();
        assert_eq!(t, "@Bob\u{2005}你好");
        assert_eq!(s.unwrap(), "<msgsource><atuserlist>wxid_bob</atuserlist><alnode><fr>1</fr></alnode></msgsource>");
        assert!(render_mentions("x", &[("notify@all".into(), "all".into())]).is_err());
        assert!(render_mentions("x", &[("a,b".into(), "x".into())]).is_err());
        let (t, _) = render_mentions("x", &[("wxid_b".into(), "bad\nname".into())]).unwrap();
        assert_eq!(t, "@wxid_b\u{2005}x");
    }
}
